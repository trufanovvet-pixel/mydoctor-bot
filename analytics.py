"""First-party product analytics. No message text, contacts, IPs or file names.

UTC database timestamps; reporting timezone is explicit. Business events are written
in the transaction that changes the business row. The journal is never payment truth.
"""
from contextvars import ContextVar
from datetime import datetime, timedelta
import logging
import secrets
from zoneinfo import ZoneInfo

from sqlalchemy import (DateTime, ForeignKey, Index, Integer, JSON, String, event,
                        select, func, case, literal, cast, and_, or_, text, inspect, Table, MetaData)
from sqlalchemy.orm import Mapped, mapped_column, Session
import storage

log = logging.getLogger(__name__)
context = ContextVar('analytics_context', default={})
SOURCES = ('telegram', 'web', 'app')
NAVIGATION = {'/history':None, '/messages':None, '/operations':None, '/profile':None, '/how-it-works':None, '/install':None, '/': None, '/dashboard': None, '/app': None, '/register': None, '/login': None,
              '/pets': 'pets_open', '/analyses': 'analyses_open', '/prevention': 'prevention_open',
              '/assistant': 'assistant_open', '/consultation': 'consultation_open', '/billing': 'pricing_open'}
FUNNEL = [('app_open', 'Зашёл'), ('registration', 'Зарегистрировался'), ('pet_created', 'Добавил питомца'),
          ('ai_question', 'Задал вопрос'), ('pricing_open', 'Посмотрел тариф'),
          ('payment_started', 'Начал оплату'), ('payment_success', 'Оплатил')]


class AnalyticsEvent(storage.Base):
    __tablename__ = 'analytics_events'
    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True, index=True)
    anonymous_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_name: Mapped[str] = mapped_column(String(40), index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    details: Mapped[dict] = mapped_column('metadata', JSON, default=dict)
    __table_args__ = (Index('ix_analytics_period_event', 'timestamp', 'event_name'),
                      Index('ix_analytics_source_period_user', 'source', 'timestamp', 'user_id'),
                      Index('ix_analytics_user_time', 'user_id', 'timestamp'))


class AnalyticsState(storage.Base):
    __tablename__ = 'analytics_state'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


def insert_rows(conn, rows):
    if not rows:
        return
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = pg_insert if conn.dialect.name == 'postgresql' else sqlite_insert
    conn.execute(insert(AnalyticsEvent.__table__).values(rows).on_conflict_do_nothing(index_elements=['event_id']))


def row(name, uid=None, *, key=None, at=None, source=None, details=None, historical=False):
    ctx = {} if historical else context.get()
    return dict(event_id=key or secrets.token_hex(16), user_id=uid,
                anonymous_id=ctx.get('anonymous_id') if not uid else None,
                session_id=ctx.get('session_id'), event_name=name,
                source=source or ctx.get('source') or 'unknown', timestamp=at or datetime.utcnow(),
                metadata=details or {})


def safe_insert(conn, rows):
    if not rows:
        return
    # A failed optional event must not roll back a pet, answer or payment.
    try:
        with conn.begin_nested():
            if conn.dialect.name == 'postgresql':
                old_timeout = conn.scalar(text('SHOW lock_timeout'))
                conn.execute(text("SET LOCAL lock_timeout = '500ms'"))
            insert_rows(conn, rows)
            if conn.dialect.name == 'postgresql':
                conn.execute(text("SELECT set_config('lock_timeout', :value, true)"), {'value':old_timeout})
    except Exception:
        log.warning('analytics event write failed', exc_info=False)


def track_batch(rows):
    if not rows:
        return
    try:
        with storage.engine.begin() as conn:
            safe_insert(conn, rows)
    except Exception:
        log.warning('analytics connection unavailable', exc_info=False)


def track(name, uid=None, **kwargs):
    track_batch([row(name, uid, **kwargs)])


def bind_identity(uid, sid):
    # Link only this login session's previously anonymous events, never another account.
    if not sid or not uid:
        return
    try:
        with storage.engine.begin() as conn:
            conn.execute(AnalyticsEvent.__table__.update().where(
                AnalyticsEvent.session_id == sid, AnalyticsEvent.user_id.is_(None)).values(user_id=uid))
    except Exception:
        log.warning('analytics identity link unavailable', exc_info=False)


def _business_events(db, flush_context):
    conn = db.connection()
    rows = []
    for obj in list(db.new) + list(db.dirty):
        name = getattr(obj, '__tablename__', '')
        fresh = obj in db.new
        if name == 'users' and fresh:
            rows.append(row('registration', obj.id, key=f'user:{obj.id}', at=obj.created_at))
        elif name == 'pets' and fresh:
            rows.append(row('pet_created', obj.user_id, key=f'pet:{obj.id}', at=obj.created_at))
        elif name == 'consultations' and fresh and obj.kind == 'consult_request':
            rows.append(row('consultation_created', obj.user_id, key=f'consult:{obj.id}', at=obj.created_at))
        elif name == 'web_documents' and fresh:
            rows.append(row('analysis_uploaded', obj.user_id, key=f'document:{obj.id}', at=obj.created_at))
        elif name == 'billing_credit_usage' and fresh:
            source = 'telegram' if obj.request_key.startswith('tg-') else None
            rows.append(row('ai_question', obj.user_id, key=f'usage:{obj.id}', at=obj.created_at,
                            source=source, details={'kind': obj.kind}))
            if source == 'telegram' and obj.kind == 'document':
                rows.append(row('analysis_uploaded', obj.user_id, key=f'tg-document:{obj.id}', at=obj.created_at, source=source))
        elif name == 'billing_payment_orders':
            if fresh:
                rows.append(row('payment_started', obj.user_id, key=f'order:{obj.id}:start', at=obj.created_at))
            changes = inspect(obj).attrs.status.history
            if (fresh or changes.has_changes()) and obj.status in ('paid', 'rejected', 'cancelled'):
                # Attribution belongs to the customer's checkout, not the approving doctor.
                source = conn.scalar(select(AnalyticsEvent.source).where(AnalyticsEvent.event_id == f'order:{obj.id}:start')) or 'unknown'
                ev = 'payment_success' if obj.status == 'paid' else 'payment_failed'
                rows.append(row(ev, obj.user_id, key=f'order:{obj.id}:{obj.status}',
                                at=obj.paid_at if obj.status == 'paid' else None,
                                source=source, details={'order_id': obj.id}, historical=True))
    safe_insert(conn, rows)


def install_hooks():
    if not event.contains(Session, 'after_flush', _business_events):
        event.listen(Session, 'after_flush', _business_events)


def migrate():
    """Additive, idempotent backfill under the same lock as existing schema startup."""
    import billing as b
    with storage.engine.begin() as conn:
        if conn.dialect.name == 'postgresql':
            conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': 728190423})
        AnalyticsEvent.__table__.create(conn, checkfirst=True)
        AnalyticsState.__table__.create(conn, checkfirst=True)
        Index('ix_billing_paid_analytics', b.PaymentOrder.status, b.PaymentOrder.paid_at).create(conn, checkfirst=True)
        if conn.scalar(select(AnalyticsState.value).where(AnalyticsState.key == 'v1')):
            return
        started = datetime.utcnow()
        def copy(query, convert):
            # Bounded batches; no medical contents or contacts are selected.
            result = conn.execute(query)
            while batch := result.fetchmany(300):
                insert_rows(conn, [convert(r) for r in batch])
        copy(select(storage.User.id, storage.User.created_at),
             lambda r: row('registration', r.id, key=f'user:{r.id}', at=r.created_at, historical=True))
        copy(select(storage.Pet.id, storage.Pet.user_id, storage.Pet.created_at),
             lambda r: row('pet_created', r.user_id, key=f'pet:{r.id}', at=r.created_at, historical=True))
        copy(select(storage.Consultation.id, storage.Consultation.user_id, storage.Consultation.created_at).where(storage.Consultation.kind == 'consult_request'),
             lambda r: row('consultation_created', r.user_id, key=f'consult:{r.id}', at=r.created_at, historical=True))
        copy(select(b.CreditUsage.id, b.CreditUsage.user_id, b.CreditUsage.created_at, b.CreditUsage.request_key, b.CreditUsage.kind),
             lambda r: row('ai_question', r.user_id, key=f'usage:{r.id}', at=r.created_at,
                           source='telegram' if r.request_key.startswith('tg-') else 'unknown',
                           details={'kind': r.kind}, historical=True))
        # Before the credit ledger existed, saved completed consultations are the evidence.
        # After that cutoff use the ledger only: Telegram has no reliable old join key.
        cutoff = conn.scalar(select(func.min(b.CreditUsage.created_at))) or started
        copy(select(storage.Consultation.id, storage.Consultation.user_id, storage.Consultation.created_at, storage.Consultation.kind).where(
            or_(and_(storage.Consultation.kind.in_(['chat', 'voice', 'file_analysis']), storage.Consultation.created_at < cutoff),
                and_(storage.Consultation.kind == 'web_chat', ~select(b.CreditUsage.id).where(
                    b.CreditUsage.kind == 'chat', b.CreditUsage.source_id == storage.Consultation.id,
                    ~b.CreditUsage.request_key.like('tg-%')).exists()))),
             lambda r: row('ai_question', r.user_id, key=f'legacy-ai:{r.id}', at=r.created_at,
                           source='unknown' if r.kind == 'web_chat' else 'telegram',
                           details={'historical_completed': True}, historical=True))
        doc = (Table('web_documents', MetaData(), autoload_with=conn)
               if inspect(conn).has_table('web_documents') else None)
        if doc is not None and inspect(conn).has_table('web_documents'):
            copy(select(doc.c.id, doc.c.user_id, doc.c.created_at),
                 lambda r: row('analysis_uploaded', r.user_id, key=f'document:{r.id}', at=r.created_at, historical=True))
        copy(select(b.PaymentOrder.id, b.PaymentOrder.user_id, b.PaymentOrder.created_at),
             lambda r: row('payment_started', r.user_id, key=f'order:{r.id}:start', at=r.created_at, historical=True))
        copy(select(b.PaymentOrder.id, b.PaymentOrder.user_id, b.PaymentOrder.paid_at).where(
            b.PaymentOrder.status == 'paid', b.PaymentOrder.paid_at.is_not(None)),
             lambda r: row('payment_success', r.user_id, key=f'order:{r.id}:paid', at=r.paid_at,
                           details={'order_id': r.id}, historical=True))
        conn.execute(AnalyticsState.__table__.insert().values(key='v1', value=started.isoformat()))
        counts = dict(conn.execute(select(AnalyticsEvent.event_name, func.count()).group_by(AnalyticsEvent.event_name)).all())
        log.warning('Analytics migration v1 complete; collection_since=%s; historical_counts=%s', started.isoformat(), counts)


def period_bounds(period, timezone='Asia/Bangkok', now=None):
    tz = ZoneInfo(timezone)
    now = now or datetime.utcnow()
    local = now.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    days = {'today': 0, '7': 6, '30': 29}
    start = ((midnight - timedelta(days=days[period])).astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
             if period in days else None)
    return start, now, tz


def report(period='today', source=None, timezone='Asia/Bangkok', now=None):
    """Aggregate in SQL, including identity deduplication and an ordered funnel."""
    import billing as b
    E = AnalyticsEvent
    start, end, tz = period_bounds(period, timezone, now)
    with storage.SessionLocal() as db:
        since_raw = db.scalar(select(AnalyticsState.value).where(AnalyticsState.key == 'v1'))
        since = datetime.fromisoformat(since_raw) if since_raw else end
        def where(start_at=start, channel=source):
            clauses = [E.timestamp <= end]
            if start_at is not None: clauses.append(E.timestamp >= start_at)
            if channel: clauses.append(E.source == channel)
            return clauses
        identity = case((E.user_id.is_not(None), literal('u:') + cast(E.user_id, String)),
                        else_=literal('a:') + E.anonymous_id)
        # Anonymous devices are explicitly separate from identified users.
        totals = db.execute(select(func.count(func.distinct(E.user_id)),
                            func.count(func.distinct(case((E.user_id.is_(None), E.anonymous_id))))).where(*where())).one()
        event_counts = dict(db.execute(select(E.event_name, func.count()).where(*where()).group_by(E.event_name)).all())
        new_conditions = [storage.User.created_at <= end]
        if start: new_conditions.append(storage.User.created_at >= start)
        if source:
            # Registration attribution only; do not invent a source from later usage.
            new_conditions.append(storage.User.id.in_(select(E.user_id).where(E.event_name == 'registration', *where())))
        new_users = db.scalar(select(func.count(storage.User.id)).where(*new_conditions)) or 0
        channels = dict(db.execute(select(E.source, func.count(func.distinct(E.user_id))).where(*where(channel=None)).group_by(E.source)).all())
        returning = db.scalar(select(func.count(func.distinct(E.user_id))).where(*where(), E.session_id.is_not(None),
            E.user_id.in_(select(E.user_id).where(E.timestamp < (start or since), E.timestamp >= since, E.session_id.is_not(None))))) or 0
        repeated = select(E.user_id).where(*where(start_at=max(start or since, since)), E.session_id.is_not(None), E.user_id.is_not(None)).group_by(E.user_id).having(func.count(func.distinct(E.session_id)) > 1).subquery()
        repeat_count = db.scalar(select(func.count()).select_from(repeated)) or 0

        # Payment truth is the existing ledger, not events submitted by a client.
        origin = select(E.event_id, E.source).where(E.event_name == 'payment_started').subquery()
        paid_conditions = [b.PaymentOrder.status == 'paid', b.PaymentOrder.paid_at.is_not(None), b.PaymentOrder.paid_at <= end]
        if start: paid_conditions.append(b.PaymentOrder.paid_at >= start)
        if source: paid_conditions.append(origin.c.source == source)
        payments = select(b.PaymentOrder).outerjoin(origin, origin.c.event_id == literal('order:') + b.PaymentOrder.id + literal(':start')).where(*paid_conditions).subquery()
        payment_count = db.scalar(select(func.count()).select_from(payments)) or 0
        payers = db.scalar(select(func.count(func.distinct(payments.c.user_id)))) or 0
        revenue = [dict(currency=r.currency, amount_minor=int(r.amount or 0), count=r.count,
                        average_minor=round((r.amount or 0) / r.count)) for r in db.execute(select(payments.c.currency,
                   func.sum(payments.c.amount_minor).label('amount'), func.count().label('count')).group_by(payments.c.currency))]
        plans = [dict(plan=b.PLANS.get(r.plan, {}).get('name', r.plan), currency=r.currency, count=r.count,
                      amount_minor=int(r.amount or 0)) for r in db.execute(select(payments.c.plan, payments.c.currency,
                 func.count().label('count'), func.sum(payments.c.amount_minor).label('amount')).group_by(payments.c.plan, payments.c.currency))]
        first_paid = select(b.PaymentOrder.user_id, func.min(b.PaymentOrder.paid_at).label('first')).where(
            b.PaymentOrder.status == 'paid', b.PaymentOrder.paid_at.is_not(None)).group_by(b.PaymentOrder.user_id).subquery()
        first_conditions = [first_paid.c.first <= end, first_paid.c.user_id.in_(select(payments.c.user_id))]
        if start: first_conditions.append(first_paid.c.first >= start)
        converted = db.scalar(select(func.count()).select_from(first_paid).where(*first_conditions)) or 0
        # Denominator: active identified people who were not paid before the period.
        eligible = db.scalar(select(func.count(func.distinct(E.user_id))).where(*where(),
            ~E.user_id.in_(select(first_paid.c.user_id).where(first_paid.c.first < (start or datetime.min))))) or 0

        # Ordered within-period funnel. Every stage belongs to the preceding cohort.
        steps = []
        previous = None
        initial = 0
        for name, label in FUNNEL:
            q = select(identity.label('person'), func.min(E.timestamp).label('at')).where(*where(), E.timestamp >= since, E.event_name == name, identity.is_not(None))
            if previous is not None:
                q = q.join(previous, and_(identity == previous.c.person, E.timestamp >= previous.c.at))
            current = q.group_by(identity).cte('funnel_' + name)
            count = db.scalar(select(func.count()).select_from(current)) or 0
            if previous is None: initial = count
            last = steps[-1]['users'] if steps else count
            steps.append(dict(name=name, label=label, users=count,
                              previous_pct=round(100*count/last, 1) if last else None,
                              total_pct=round(100*count/initial, 1) if initial else None))
            previous = current

        earliest = db.scalar(select(func.min(E.timestamp))) or end
        chart_start = start or earliest
        span = (end - chart_start).days
        granularity = 'hour' if period == 'today' else 'month' if period == 'all' and span > 120 else 'day'
        def bucket(col):
            if db.bind.dialect.name == 'postgresql':
                local_col = func.timezone(timezone, func.timezone('UTC', col))
                return func.to_char(local_col, {'hour':'YYYY-MM-DD HH24:00', 'day':'YYYY-MM-DD', 'month':'YYYY-MM'}[granularity])
            offset = end.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz).utcoffset().total_seconds()/3600
            return func.strftime({'hour':'%Y-%m-%d %H:00', 'day':'%Y-%m-%d', 'month':'%Y-%m'}[granularity], col, f'{offset:+g} hours')
        activity = {r.bucket: dict(users=r.users, ai=r.ai) for r in db.execute(select(bucket(E.timestamp).label('bucket'),
            func.count(func.distinct(E.user_id)).label('users'), func.sum(case((E.event_name == 'ai_question', 1), else_=0)).label('ai')).where(*where()).group_by('bucket'))}
        paid_series = {}
        for r in db.execute(select(bucket(payments.c.paid_at).label('bucket'), payments.c.currency,
                                   func.count().label('count'), func.sum(payments.c.amount_minor).label('amount')).group_by('bucket', payments.c.currency)):
            point = paid_series.setdefault(r.bucket, {'payments':0, 'revenue':{}})
            point['payments'] += r.count
            point['revenue'][r.currency] = int(r.amount or 0)/100
        local_start = chart_start.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz).replace(minute=0, second=0, microsecond=0)
        local_end = end.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz)
        if granularity != 'hour': local_start = local_start.replace(hour=0)
        if granularity == 'month': local_start = local_start.replace(day=1)
        series = []
        fmt = {'hour':'%Y-%m-%d %H:00', 'day':'%Y-%m-%d', 'month':'%Y-%m'}[granularity]
        while local_start <= local_end:
            key = local_start.strftime(fmt)
            series.append(dict(label=key, **activity.get(key, {'users':0,'ai':0}), **paid_series.get(key, {'payments':0,'revenue':{}})))
            if granularity == 'month': local_start = (local_start.replace(day=28)+timedelta(days=4)).replace(day=1)
            else: local_start += timedelta(hours=1) if granularity == 'hour' else timedelta(days=1)
        registered_total = db.scalar(select(func.count(storage.User.id))) or 0
        return dict(period=period, source=source, timezone=timezone, since=since.isoformat(),
                    active_users=totals[0], anonymous_visitors=totals[1], new_users=new_users, registered_total=registered_total,
                    channels=channels, ai_questions=event_counts.get('ai_question',0), consultations=event_counts.get('consultation_created',0),
                    payments=payment_count, payers=payers, revenue=revenue, plans=plans,
                    payment_conversion=round(payers*100/totals[0],1) if totals[0] else None,
                    first_payers=converted, free_to_paid_pct=round(converted*100/eligible,1) if eligible else None,
                    returning_users=returning, repeat_users=repeat_count, funnel=steps, series=series,
                    granularity=granularity, events=event_counts)
