"""Opt-in, leased medication reminders; all actions re-check shared-pet access."""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, BadRequest, RetryAfter, NetworkError
import storage
import pet_care as care
from care_models import CareNoticeAttempt, CareNotice, CarePreference, MedicationCourse, MedicationDose

log = logging.getLogger(__name__)
LABEL = '💊 Уход и дневник'


def telegram_destination(db, uid):
    """Use only a server-verified identity, never email/name matching."""
    user = db.get(storage.User, uid)
    if user and user.telegram_id > 0:
        return user.telegram_id
    from doctor_access import DoctorWebAccount
    from notification_patch import _admin_user_id
    binding = db.get(DoctorWebAccount, uid)
    if user and binding and binding.telegram_id == _admin_user_id():
        return binding.telegram_id
    return None


def audit_oct30_incident():
    """Bounded, read-only incident snapshot for the owner's Grom; expires Nov 1."""
    if datetime.utcnow() >= datetime(2026, 11, 1):
        return
    from billing import is_owner_account
    from web_push import PushSubscription
    from doctor_access import DoctorWebAccount
    from sqlalchemy import or_
    from notification_patch import _admin_user_id
    admin = _admin_user_id()
    with storage.SessionLocal() as db:
        users = db.scalars(select(storage.User).where(or_(storage.User.telegram_id == admin,
            storage.User.id.in_(select(DoctorWebAccount.user_id).where(DoctorWebAccount.telegram_id == admin))))).all()
        for user in users:
            pets = db.scalars(select(storage.Pet).where(storage.Pet.user_id == user.id,
                storage.Pet.name.in_(['Гром', 'ГРОМ', 'гром', 'ТЕСТ Гром']))).all()
            for pet in pets:
                pref = db.get(CarePreference, (pet.id, user.id))
                log.warning('Care incident 20261030 user=%s pet=%s direct_telegram=%s verified_telegram=%s reminders=%s',
                    user.id, pet.id, user.telegram_id > 0, bool(telegram_destination(db,user.id)), bool(pref and pref.reminders))
                doses = db.execute(select(MedicationDose, MedicationCourse).join(MedicationCourse,
                    MedicationCourse.id == MedicationDose.course_id).where(MedicationCourse.pet_id == pet.id,
                    MedicationDose.due_at >= datetime(2026,10,29,17), MedicationDose.due_at < datetime(2026,10,30,17))
                    .order_by(MedicationDose.due_at).limit(30)).all()
                for dose, course in doses:
                    rows = db.scalars(select(CareNotice).where(CareNotice.dose_id == dose.id, CareNotice.user_id == user.id)).all()
                    log.warning('Care incident 20261030 dose=%s due_utc=%s timezone=%s dose_state=%s notices=%s',
                        dose.id,dose.due_at.isoformat(),course.timezone,dose.state,
                        [(r.id,r.channel,r.state,r.attempts) for r in rows])

def enqueue_due(now=None):
    from web_push import PushSubscription
    now = now or datetime.utcnow()
    # Newly supported verified web-account bindings must not replay old doses.
    cutoff_key = 'care_verified_telegram_enabled_at'
    cutoff = storage.get_bot_setting(cutoff_key)
    if not cutoff:
        cutoff = now.isoformat()
        storage.set_bot_setting(cutoff_key, cutoff)
    linked_cutoff = datetime.fromisoformat(cutoff)
    with storage.SessionLocal() as db:
        rows = db.execute(select(MedicationDose, MedicationCourse).join(MedicationCourse,
            MedicationCourse.id == MedicationDose.course_id).where(MedicationCourse.active.is_(True),
            MedicationDose.state == 'pending', MedicationDose.due_at <= now,
            MedicationDose.due_at > now-timedelta(hours=1))).all()
        for dose, course in rows:
            pet = db.get(storage.Pet, course.pet_id)
            prefs = db.scalars(select(CarePreference).where(CarePreference.pet_id == course.pet_id,
                CarePreference.reminders.is_(True))).all()
            for pref in prefs:
                if not care.can_access(db, pet, pref.user_id): continue
                user = db.get(storage.User, pref.user_id)
                target = telegram_destination(db, pref.user_id)
                if user and user.telegram_id <= 0 and dose.due_at < linked_cutoff: target = None
                destinations = [('telegram', str(target), '')] if target else []
                destinations += [('push', sub.id, sub.binding) for sub in db.scalars(select(PushSubscription).where(
                    PushSubscription.role == 'owner', PushSubscription.subject_id == pref.user_id,
                    PushSubscription.expires_at > now)).all()]
                for channel, destination, binding in destinations:
                    existing = db.scalar(select(CareNotice.id).where(CareNotice.dose_id == dose.id,
                        CareNotice.user_id == pref.user_id, CareNotice.channel == channel, CareNotice.destination == destination))
                    if existing: continue
                    try:
                        with db.begin_nested():
                            db.add(CareNotice(dose_id=dose.id, user_id=pref.user_id, channel=channel,
                                destination=destination, binding=binding, next_attempt=now)); db.flush()
                    except IntegrityError: pass
        db.commit()


def claim_notice(now=None, channel=None):
    now = now or datetime.utcnow()
    with storage.SessionLocal() as db:
        # A crashed in-flight send has an unknown outcome. Replaying it could
        # duplicate an already accepted Telegram message (no idempotency API).
        db.execute(update(CareNotice).where(CareNotice.state == 'sending',
            CareNotice.next_attempt <= now).values(state='uncertain'))
        query = select(CareNotice).where(CareNotice.state.in_(('pending', 'retry')), CareNotice.next_attempt <= now)
        if channel: query = query.where(CareNotice.channel == channel)
        row = db.scalar(query.order_by(CareNotice.next_attempt, CareNotice.id).limit(1))
        if not row: db.commit(); return None
        if not row: return None
        changed = db.execute(update(CareNotice).where(CareNotice.id == row.id,
            CareNotice.state.in_(('pending', 'retry')), CareNotice.next_attempt <= now).values(
                state='sending', attempts=CareNotice.attempts+1, next_attempt=now+timedelta(minutes=2)),
            execution_options={'synchronize_session': False})
        if changed.rowcount != 1: db.rollback(); return None
        result = row.id; db.commit(); return result


def notice_payload(nid, now=None):
    from web_push import PushSubscription
    now = now or datetime.utcnow()
    with storage.SessionLocal() as db:
        row = db.get(CareNotice, nid)
        if not row or row.state != 'sending': return None
        dose = db.get(MedicationDose, row.dose_id)
        course = db.get(MedicationCourse, dose.course_id) if dose else None
        pet = db.get(storage.Pet, course.pet_id) if course else None
        pref = db.get(CarePreference, (pet.id, row.user_id)) if pet else None
        valid = (dose and dose.state == 'pending' and now-timedelta(hours=1) < dose.due_at <= now
            and course.active and pref and pref.reminders and care.can_access(db, pet, row.user_id))
        subscription = None
        user = db.get(storage.User, row.user_id)
        lang = 'ru'
        from owner_profile import OwnerProfile
        profile = db.get(OwnerProfile, row.user_id)
        if profile: lang = profile.language
        if valid and row.channel == 'telegram':
            target = telegram_destination(db, row.user_id)
            valid = bool(target and str(target) == row.destination)
        elif valid and row.channel == 'push':
            sub = db.get(PushSubscription, row.destination)
            valid = bool(sub and sub.role == 'owner' and sub.subject_id == row.user_id
                and sub.binding == row.binding and sub.expires_at > now)
            if valid:
                subscription = dict(endpoint=sub.endpoint, keys=dict(p256dh=sub.p256dh, auth=sub.auth)); lang = sub.language
        else: valid = False
        if not valid:
            row.state = 'skipped'; db.commit(); return None
        from care_delivery_probe import mode
        test_mode = mode(db, row, dose, course)
        return dict(test_mode=test_mode, id=nid, dose_id=dose.id, pet_id=pet.id, channel=row.channel, destination=row.destination,
            subscription=subscription, language=lang, time=care.local_label(dose.due_at, course.timezone), timezone=course.timezone)


def finish_notice(nid, state, provider_code='', message_id='', retry_after=None):
    with storage.SessionLocal() as db:
        row = db.get(CareNotice, nid)
        if not row or row.state != 'sending': return
        row.state = 'failed' if state == 'retry' and row.attempts >= 5 else state
        delay = min(600, 30*2**min(row.attempts,4)) if retry_after is None else max(1, retry_after)
        row.next_attempt = datetime.utcnow()+timedelta(seconds=delay)
        db.add(CareNoticeAttempt(notice_id=nid, attempt=row.attempts, outcome=row.state,
            provider_code=provider_code[:80], message_id=str(message_id)[:40]))
        log.warning('Care delivery notice=%s dose=%s user=%s channel=%s attempt=%s outcome=%s provider=%s message_id=%s',
            nid, row.dose_id, row.user_id, row.channel, row.attempts, row.state, provider_code, message_id)
        db.commit()


def send_push(payload):
    from web_push import NoRedirectSession, vapid_keys, webpush, WebPushException
    try:
        with NoRedirectSession() as transport:
            result = webpush(subscription_info=payload['subscription'], data=json.dumps(dict(
                language=payload['language'], kind='care', test=bool(payload.get('test_mode')), tag='mydoctor-dose-'+str(payload['dose_id']),
                url='/care/pets/'+str(payload['pet_id'])+'#medications')),
                vapid_private_key=vapid_keys()['private'], vapid_claims={'sub':os.getenv('MYDOCTOR_WEB_URL','https://mydoctor.vet')},
                ttl=900, timeout=10, requests_session=transport)
            return 'delivered' if 200 <= result.status_code < 300 else 'retry'
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else 0
        return 'failed' if status in (400,401,403,404,410) else 'retry'


async def deliver_care_notifications(application):
    await asyncio.to_thread(enqueue_due)
    # Each channel has its own claim, outcome and retry; a slow provider cannot
    # starve the other channel.
    await asyncio.gather(_deliver_channel(application, 'telegram'), _deliver_channel(application, 'push'))


async def _deliver_channel(application, channel):
    for _ in range(40):
        nid = await asyncio.to_thread(claim_notice, channel=channel)
        if nid is None: break
        payload = await asyncio.to_thread(notice_payload, nid)
        if not payload: continue
        state = 'retry'
        provider_code = ''; message_id = ''; retry_after = None
        try:
            if payload['channel'] == 'push':
                state = await asyncio.to_thread(send_push, payload)
                provider_code = 'push_accepted' if state == 'delivered' else 'push_error'
            else:
                if payload.get('test_mode') == 'retry':
                    # Inject only for the persisted, owner-authorized test dose.
                    # No request reaches Telegram on this deliberately failed attempt.
                    raise RetryAfter(1)
                english = payload['language'] == 'en'
                text = ('A prescribed medicine is scheduled for '+payload['time']+' ('+payload['timezone']+'). '
                    'Open the care diary and check the latest family entry before giving it. '
                    'For a missed dose, follow your veterinarian’s instructions. /care' if english else
                    'Приём лекарства по назначению запланирован на '+payload['time']+' ('+payload['timezone']+'). '
                    'Откройте дневник и проверьте последнюю отметку семьи перед приёмом. '
                    'При пропуске следуйте указаниям врача. Открыть дневник: /care')
                if payload.get('test_mode'):
                    text = ('TEST notification. Do not give any medicine. ' if english else
                        'ТЕСТ уведомления. Никакие лекарства давать не нужно. ') + payload['time'] + ' (' + payload['timezone'] + ')'
                # No blind "given" button in the message: the actual drug and dose must be reviewed in the diary.
                result = await application.bot.send_message(chat_id=int(payload['destination']), text=text,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Open diary' if english else 'Открыть дневник',
                        url='https://mydoctor.vet/care/pets/'+str(payload['pet_id'])+'#medications')]]),
                    read_timeout=15, write_timeout=15, connect_timeout=10)
                state = 'delivered'; provider_code = 'telegram_ok'
                message_id = getattr(result, 'message_id', '')
        except Forbidden:
            state = 'blocked'; provider_code = 'telegram_forbidden'
        except BadRequest:
            state = 'failed'; provider_code = 'telegram_bad_request'
        except RetryAfter as exc:
            state = 'retry'; provider_code = 'telegram_retry_after'
            retry_after = exc.retry_after.total_seconds() if isinstance(exc.retry_after, timedelta) else float(exc.retry_after)
        except NetworkError:
            # The request may have reached Telegram before the connection failed.
            state = 'uncertain'; provider_code = 'telegram_network_unknown'
        except Exception as exc:
            provider_code = type(exc).__name__
            log.warning('Care reminder %s: %s', nid, provider_code)
        await asyncio.to_thread(finish_notice, nid, state, provider_code, message_id, retry_after)


async def care_loop(application):
    try: await asyncio.to_thread(audit_oct30_incident)
    except Exception as exc: log.warning('Care incident snapshot unavailable: %s', type(exc).__name__)
    try:
        from care_delivery_probe import seed
        await asyncio.to_thread(seed)
    except Exception as exc: log.warning('Care probe unavailable: %s', type(exc).__name__)
    log.warning('Care reminder worker started: independent telegram/push channels')
    while True:
        try: await deliver_care_notifications(application)
        except Exception as exc: log.error('Care reminder worker: %s', type(exc).__name__)
        await asyncio.sleep(30)


async def care_command(update, context):
    import web_auth
    await asyncio.to_thread(storage.ensure_user, update.effective_user.id,
        update.effective_user.username, update.effective_user.first_name)
    token = await asyncio.to_thread(web_auth.create_token, update.effective_user.id)
    await update.effective_message.reply_text('Уход за питомцем: назначения, дневник и семейные отметки. '
        'Ссылка для входа действует 10 минут. Не пересылайте её другим людям. Для семьи создайте отдельное приглашение в кабинете.',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Открыть уход и дневник',
            url='https://mydoctor.vet/login/'+token+'?next=/care')]]))


def install(bot):
    from telegram import ReplyKeyboardMarkup
    original = bot.message
    rows = [list(row) for row in bot.MENU.keyboard]
    if not any(getattr(button, 'text', button) == LABEL for row in rows for button in row):
        rows.append([LABEL]); bot.MENU = ReplyKeyboardMarkup(rows, resize_keyboard=True)
    async def message(update, context):
        if update.message and update.message.text == LABEL: return await care_command(update, context)
        return await original(update, context)
    bot.message = message
