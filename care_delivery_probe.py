"""One-off, owner-authorized Oct 30 delivery probe. Expires automatically."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
import storage
from care_models import CarePreference, MedicationCourse, MedicationDose

KEY = 'care_delivery_probe_20261030_v1'


def seed():
    from care_notifications import telegram_destination, log
    from notification_patch import _admin_user_id
    from web_push import PushSubscription
    now = datetime.utcnow()
    if now >= datetime(2026, 10, 31): return
    with storage.SessionLocal() as db:
        # Incident evidence identified this owner's web account. Never fall back
        # to an arbitrary user or recreate/modify a real animal's treatment.
        user = db.scalar(select(storage.User).where(storage.User.id == 4).with_for_update())
        if not user or telegram_destination(db, user.id) != _admin_user_id(): return
        if db.get(storage.BotSetting, KEY): return
        if not db.scalar(select(PushSubscription.id).where(PushSubscription.role == 'owner',
                PushSubscription.subject_id == user.id, PushSubscription.expires_at > now).limit(1)):
            log.warning('Care probe blocked: no active owner push subscription'); return
        pet = storage.Pet(user_id=user.id, name='ТЕСТ доставки 30.10, не лечить', species='Собака')
        db.add(pet); db.flush()
        db.add(CarePreference(pet_id=pet.id,user_id=user.id,reminders=True))
        metadata = dict(user_id=user.id,pet_id=pet.id,course_ids=[],dose_ids=[],times=[])
        for index in range(2):
            due = (now + timedelta(minutes=2+index*2)).replace(second=0,microsecond=0)
            local = due.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Bangkok'))
            course = MedicationCourse(pet_id=pet.id,created_by=user.id,
                request_key=f'care-probe-20261030-{index}',
                name='ТЕСТ доставки, лекарство не давать',dose='ТЕСТ. Не давать никаких препаратов.',
                instructions='Проверка двух каналов уведомлений, не медицинское назначение.',
                timezone='Asia/Bangkok',times=[local.strftime('%H:%M')],
                start_date=local.date(),end_date=local.date())
            db.add(course); db.flush()
            dose = MedicationDose(course_id=course.id,due_at=due)
            db.add(dose); db.flush()
            metadata['course_ids'].append(course.id)
            metadata['dose_ids'].append(dose.id)
            metadata['times'].append(local.isoformat())
        db.add(storage.BotSetting(key=KEY,value=json.dumps(metadata)))
        db.commit()
        log.warning('Care probe created: %s',json.dumps(metadata))


def mode(db, row, dose, course):
    marker = db.get(storage.BotSetting, KEY)
    if not marker: return ''
    data = json.loads(marker.value)
    if row.user_id != data['user_id'] or course.pet_id != data['pet_id'] or dose.id not in data['dose_ids']: return ''
    return 'retry' if dose.id == data['dose_ids'][1] and row.attempts == 1 else 'normal'
