"""Manual payment orders and an atomic, auditable credit ledger.

Payments remain unavailable until the doctor configures a receiving method and
enables sales. No bank credentials or exchange API keys are needed or stored.
"""
import hashlib
import json
import secrets
import re
from decimal import Decimal
from datetime import datetime, timedelta

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, select, update, text as sql_text
from sqlalchemy.orm import Mapped, mapped_column
import storage

PLANS = {
    'start': {'name': 'Старт', 'credits': 20, 'RUB': 49900, 'USD': 700, 'USDT': 700},
    'care': {'name': 'Забота', 'credits': 60, 'RUB': 99000, 'USD': 1400, 'USDT': 1400},
    'family': {'name': 'Семья', 'credits': 150, 'RUB': 199000, 'USD': 2900, 'USDT': 2900},
}
ORDER_STATES = {'awaiting': 'Ожидает перевода', 'review': 'Проверяем оплату', 'paid': 'Оплата подтверждена',
                'rejected': 'Оплата не подтверждена', 'cancelled': 'Отменено'}
TRIAL_CREDITS = 5
CARD_TRANSFERS = {
    'mir': {'name': 'Российская карта', 'aliases': ('Перевод на карту Мир',), 'mark': 'Карта', 'currencies': ('RUB',), 'default_currency': 'RUB'},
    'sbp': {'name': 'СБП', 'mark': 'СБП', 'currencies': ('RUB',), 'default_currency': 'RUB'},
    'mastercard': {'name': 'Перевод на Mastercard', 'mark': 'Mastercard', 'currencies': ('BYN', 'KZT', 'USD', 'RUB', 'EUR'), 'default_currency': 'BYN'},
}


def route_names(route):
    return (route['name'], *route.get('aliases', ()))


def method_route(name):
    return next((v for v in CARD_TRANSFERS.values() if name in route_names(v)), None)


def init_schema():
    # Gunicorn workers must not race PostgreSQL's check-then-create DDL on first boot.
    # The transaction-scoped lock is released on both commit and rollback.
    with storage.engine.begin() as connection:
        if connection.dialect.name == 'postgresql':
            connection.execute(sql_text('SELECT pg_advisory_xact_lock(:key)'), {'key': 728190423})
        storage.Base.metadata.create_all(connection)


class PaymentMethod(storage.Base):
    __tablename__ = 'billing_payment_methods'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(8))
    network: Mapped[str | None] = mapped_column(String(80), nullable=True)
    instructions: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PaymentOrder(storage.Base):
    __tablename__ = 'billing_payment_orders'
    __table_args__ = (UniqueConstraint('user_id', 'request_key'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    request_key: Mapped[str] = mapped_column(String(64))
    plan: Mapped[str] = mapped_column(String(32))
    credits: Mapped[int] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8))
    method_id: Mapped[int] = mapped_column(ForeignKey('billing_payment_methods.id'))
    method_name: Mapped[str] = mapped_column(String(80))
    instructions: Mapped[str] = mapped_column(Text)
    network: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='awaiting', index=True)
    payment_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    reported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_by: Mapped[str | None] = mapped_column(String(40), nullable=True)


class PaymentMethodPrice(storage.Base):
    __tablename__ = 'billing_payment_method_prices'
    method_id: Mapped[int] = mapped_column(ForeignKey('billing_payment_methods.id'), primary_key=True)
    plan: Mapped[str] = mapped_column(String(32), primary_key=True)
    amount_minor: Mapped[int] = mapped_column(Integer)


def method_prices(db, method):
    if not method or not method.instructions.strip():
        return {}
    route = method_route(method.name)
    if not route or method.currency not in route['currencies']:
        return {}
    if method.currency == 'RUB':
        return {key: value['RUB'] for key, value in PLANS.items()}
    prices = {p.plan: p.amount_minor for p in db.scalars(select(PaymentMethodPrice).where(PaymentMethodPrice.method_id == method.id))}
    return prices if all(isinstance(prices.get(key), int) and 0 < prices[key] <= 100_000_000 for key in PLANS) else {}


def card_methods(db, include_disabled=False):
    result = {}
    for key, route in CARD_TRANSFERS.items():
        method = db.scalar(select(PaymentMethod).where(PaymentMethod.name.in_(route_names(route))).order_by(PaymentMethod.id.desc()).limit(1))
        prices = method_prices(db, method)
        if method and (include_disabled or (method.enabled and prices)):
            result[key] = {'method': method, 'prices': prices}
    return result


def validate_card_method(route_key, currency, instructions, amounts):
    route = CARD_TRANSFERS.get(route_key)
    if not route or currency not in route['currencies'] or not 10 <= len(instructions) <= 2000:
        raise BillingError('Проверьте валюту и реквизиты получателя.')
    prices = {}
    if currency != 'RUB':
        for key in PLANS:
            value = str(amounts.get(key, '')).strip().replace(',', '.')
            if not re.fullmatch(r'\d{1,7}(?:\.\d{1,2})?', value):
                raise BillingError('Укажите цену каждого пакета в валюте получателя: больше нуля, не более двух знаков после запятой.')
            amount = int(Decimal(value) * 100)
            if not 0 < amount <= 100_000_000:
                raise BillingError('Укажите цену каждого пакета в валюте получателя: больше нуля, не более двух знаков после запятой.')
            prices[key] = amount
    return route, prices


def write_card_method(db, route, currency, instructions, prices):
    db.execute(update(PaymentMethod).where(PaymentMethod.name.in_(route_names(route))).values(enabled=False))
    method = PaymentMethod(name=route['name'], currency=currency, instructions=instructions)
    db.add(method)
    db.flush()
    for key, amount in prices.items():
        db.add(PaymentMethodPrice(method_id=method.id, plan=key, amount_minor=amount))
    return method.id


def save_card_method(route_key, currency, instructions, amounts):
    route, prices = validate_card_method(route_key, currency, instructions, amounts)
    with storage.SessionLocal() as db:
        # Serialize settings changes. Existing orders keep their own receiving details and price.
        if db.bind.dialect.name == 'postgresql':
            db.execute(sql_text('SELECT pg_advisory_xact_lock(:key)'), {'key': 728190424})
        method_id = write_card_method(db, route, currency, instructions, prices)
        db.commit()
        return method_id


class CreditGrant(storage.Base):
    __tablename__ = 'billing_credit_grants'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    origin: Mapped[str] = mapped_column(String(80), unique=True)
    total: Mapped[int] = mapped_column(Integer)
    remaining: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CreditUsage(storage.Base):
    __tablename__ = 'billing_credit_usage'
    __table_args__ = (UniqueConstraint('user_id', 'request_key'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    request_key: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default='pending')
    credits: Mapped[int] = mapped_column(Integer, default=0)
    allocation: Mapped[str] = mapped_column(Text, default='[]')
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BillingError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def live():
    return storage.get_bot_setting('billing_live') == '1'


def money(minor, currency):
    return f'{minor // 100:,}'.replace(',', ' ') + (f'.{minor % 100:02d}' if minor % 100 else '') + ' ' + ('₽' if currency == 'RUB' else currency)


def lock_user(db, uid):
    # A real row write serializes financial changes on both PostgreSQL and SQLite.
    if db.execute(update(storage.User).where(storage.User.id == uid).values(id=uid)).rowcount != 1:
        raise BillingError('Аккаунт не найден.', 401)


def trial(db, uid):
    if not db.scalar(select(CreditGrant.id).where(CreditGrant.origin == f'trial:{uid}')):
        db.add(CreditGrant(user_id=uid, origin=f'trial:{uid}', total=TRIAL_CREDITS, remaining=TRIAL_CREDITS))
        db.flush()


def refundable_failure(db, usage):
    if usage.status != 'pending':
        return
    for gid, amount in json.loads(usage.allocation):
        db.execute(update(CreditGrant).where(CreditGrant.id == gid, CreditGrant.user_id == usage.user_id)
                   .values(remaining=CreditGrant.remaining + amount))
    usage.status = 'failed'
    usage.finished_at = datetime.utcnow()


def release_stale(db, uid):
    old = db.scalars(select(CreditUsage).where(CreditUsage.user_id == uid, CreditUsage.status == 'pending',
                    CreditUsage.created_at < datetime.utcnow() - timedelta(minutes=10))).all()
    for item in old:
        refundable_failure(db, item)
    db.flush()


def metered(db, uid):
    return live() or bool(db.scalar(select(CreditGrant.id).where(CreditGrant.user_id == uid, CreditGrant.origin.like('payment:%')).limit(1)))


def reserve(uid, kind, key, content):
    if kind not in ('chat', 'document'):
        raise ValueError('Unsupported AI action')
    key = key or secrets.token_hex(16)
    if not isinstance(key, str) or not 8 <= len(key) <= 64 or not key.isascii() or not all(c.isalnum() or c in '-_' for c in key):
        raise BillingError('Некорректный идентификатор запроса.')
    fingerprint = hashlib.sha256(content).hexdigest()
    with storage.SessionLocal() as db:
        lock_user(db, uid)
        release_stale(db, uid)
        previous = db.scalar(select(CreditUsage).where(CreditUsage.user_id == uid, CreditUsage.request_key == key))
        if previous:
            if previous.kind != kind or previous.fingerprint != fingerprint:
                raise BillingError('Этот запрос уже использован для другого действия.', 409)
            if previous.status == 'success':
                return previous, True
            if previous.status == 'pending':
                raise BillingError('Запрос ещё обрабатывается. Подождите и повторите попытку.', 409)
            # A failed attempt can be retried with the same key, without double debit.
            usage = previous
        else:
            usage = CreditUsage(id=secrets.token_hex(16), user_id=uid, request_key=key, fingerprint=fingerprint, kind=kind)
        cost = (1 if kind == 'chat' else 5) if metered(db, uid) else 0
        allocation = []
        if cost:
            trial(db, uid)
            lots = db.scalars(select(CreditGrant).where(CreditGrant.user_id == uid, CreditGrant.remaining > 0,
                       (CreditGrant.expires_at.is_(None)) | (CreditGrant.expires_at > datetime.utcnow()))
                       .order_by(CreditGrant.expires_at.is_(None), CreditGrant.expires_at, CreditGrant.id)).all()
            if sum(lot.remaining for lot in lots) < cost:
                # Persist crash refunds even when the new request cannot proceed.
                db.commit()
                raise BillingError('Недостаточно баллов. Откройте «Тариф и оплаты», чтобы пополнить пакет.', 402)
            left = cost
            for lot in lots:
                amount = min(left, lot.remaining)
                if amount:
                    lot.remaining -= amount
                    allocation.append([lot.id, amount])
                    left -= amount
                if not left:
                    break
        usage.credits = cost
        usage.allocation = json.dumps(allocation)
        usage.status = 'pending'
        usage.created_at = datetime.utcnow()
        usage.finished_at = None
        if previous is None:
            db.add(usage)
        db.commit()
        return usage, False


def complete(db, uid, usage_id, source_id, response):
    lock_user(db, uid)
    usage = db.get(CreditUsage, usage_id)
    if not usage or usage.user_id != uid or usage.status != 'pending':
        raise BillingError('Время обработки истекло. Повторите запрос.', 409)
    usage.status = 'success'
    usage.source_id = source_id
    usage.finished_at = datetime.utcnow()
    model = getattr(response, 'model', None)
    usage.model = model[:120] if isinstance(model, str) else 'gpt-5.6-sol'
    tokens = getattr(response, 'usage', None)
    for field in ('input_tokens', 'output_tokens'):
        number = getattr(tokens, field, None)
        setattr(usage, field, number if type(number) is int and number >= 0 else None)


def fail(uid, usage_id):
    with storage.SessionLocal() as db:
        lock_user(db, uid)
        usage = db.get(CreditUsage, usage_id)
        if usage and usage.user_id == uid:
            refundable_failure(db, usage)
        db.commit()


def summary(db, uid):
    now = datetime.utcnow()
    grants = db.scalars(select(CreditGrant).where(CreditGrant.user_id == uid).order_by(CreditGrant.created_at.desc())).all()
    active = [x for x in grants if x.expires_at is None or x.expires_at > now]
    requests = db.scalar(select(func.count(CreditUsage.id)).where(CreditUsage.user_id == uid, CreditUsage.status == 'success'))
    reserved = db.scalar(select(func.coalesce(func.sum(CreditUsage.credits), 0)).where(CreditUsage.user_id == uid, CreditUsage.status == 'pending'))
    return {'balance': sum(x.remaining for x in active), 'reserved': reserved, 'requests': requests,
            'grants': grants, 'metered': metered(db, uid)}


def create_order(uid, plan_id, method_id, key):
    if plan_id not in PLANS or not key or len(key) > 64:
        raise BillingError('Выберите тариф и способ оплаты.')
    with storage.SessionLocal() as db:
        lock_user(db, uid)
        previous = db.scalar(select(PaymentOrder).where(PaymentOrder.user_id == uid, PaymentOrder.request_key == key))
        if previous:
            if previous.plan != plan_id or previous.method_id != method_id:
                raise BillingError('Обновите страницу перед выбором другого тарифа.', 409)
            return previous.id
        if not live():
            raise BillingError('Приём оплат пока не открыт.', 409)
        method = db.get(PaymentMethod, method_id, with_for_update=True)
        prices = method_prices(db, method)
        if not method or not method.enabled or not prices:
            raise BillingError('Этот способ оплаты недоступен.', 409)
        pending = db.scalar(select(func.count(PaymentOrder.id)).where(PaymentOrder.user_id == uid,
                         PaymentOrder.status.in_(['awaiting', 'review'])))
        if pending >= 3:
            raise BillingError('У вас уже есть незавершённые заявки на оплату. Откройте их в истории.', 429)
        plan = PLANS[plan_id]
        order = PaymentOrder(id=secrets.token_hex(16), user_id=uid, request_key=key, plan=plan_id, credits=plan['credits'],
                    amount_minor=prices[plan_id], currency=method.currency, method_id=method.id,
                    method_name=method.name, instructions=method.instructions, network=method.network)
        db.add(order)
        db.commit()
        return order.id


def confirm_order(uid, oid, actor):
    with storage.SessionLocal() as db:
        lock_user(db, uid)
        order = db.get(PaymentOrder, oid)
        if not order or order.user_id != uid:
            raise BillingError('Заявка не найдена.', 404)
        if order.status == 'paid':
            return  # Idempotent even after another worker has confirmed it.
        if order.status != 'review':
            raise BillingError('Подтвердить можно только заявку, отправленную на проверку.', 409)
        now = datetime.utcnow()
        order.status = 'paid'
        order.paid_at = now
        order.checked_by = str(actor)
        db.add(CreditGrant(user_id=uid, origin='payment:' + oid, total=order.credits,
                           remaining=order.credits, expires_at=now + timedelta(days=30)))
        db.commit()
