"""Care permissions, exact prescribed schedules and owner-reported observations."""
import hashlib
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import or_, select, update
import storage
from care_models import CareMember, MedicationCourse, MedicationDose

APPETITE = {'normal': 'Обычный', 'reduced': 'Снижен', 'none': 'Не ест', 'unknown': 'Не оценивал(а)'}
ACTIVITY = {'normal': 'Обычная', 'reduced': 'Снижена', 'very_low': 'Резкая слабость', 'unknown': 'Не оценивал(а)'}
PAIN = {'none': 'Не замечаю признаков боли', 'some': 'Возможна боль', 'severe': 'Сильная или нарастающая боль', 'unknown': 'Не могу оценить'}
WOUND = {'not_applicable': 'Не оценивал(а) / нет шва', 'unchanged': 'Без заметных изменений', 'worse': 'Нарастает покраснение или отёк', 'discharge': 'Гной, запах или продолжающиеся выделения', 'open': 'Края шва разошлись'}
FLAGS = {'breathing': 'Затруднённое дыхание', 'collapse': 'Потеря сознания / коллапс', 'bleeding': 'Сильное продолжающееся кровотечение', 'urine': 'Не может помочиться', 'seizure': 'Продолжающийся приступ или повторные приступы без восстановления'}
STATES = {'pending': 'Нет отметки', 'taken': 'Дал(а)', 'skipped': 'Пропустил(а)', 'cancelled': 'Курс остановлен'}
URGENCY = {
    'urgent': 'Нужна экстренная ветеринарная помощь. Свяжитесь с ближайшей клиникой и выезжайте сейчас. Не ждите ответа в дневнике.',
    'contact': 'Свяжитесь с лечащим ветеринаром сегодня и сообщите об этих изменениях. При быстром ухудшении обращайтесь в ближайшую клинику сразу.',
    'recorded': 'Наблюдение сохранено в дневнике питомца.',
}
# Source for postoperative escalation: ACVS, Postoperative Care After Surgery.
# https://www.acvs.org/small-animal/postoperative-care-after-surgery-what-animal-owners-should-expect/


def accessible_ids(uid):
    return select(storage.Pet.id).where(or_(storage.Pet.user_id == uid,
        storage.Pet.id.in_(select(CareMember.pet_id).where(CareMember.user_id == uid))))


def can_access(db, pet, uid):
    return bool(pet and uid and (pet.user_id == uid or db.get(CareMember, (pet.id, uid))))


def get_pet(db, pid, uid, *, owner=False, lock=False):
    query = select(storage.Pet).where(storage.Pet.id == pid)
    pet = db.scalar(query.with_for_update() if lock else query)
    return pet if pet and (pet.user_id == uid if owner else can_access(db, pet, uid)) else None


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def request_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', value):
        raise ValueError('Форма устарела. Обновите страницу.')
    return value


def zone(value):
    try:
        if not value or len(value) > 80: raise ValueError()
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Выберите часовой пояс.') from None


def utc_local(local, tz):
    first = local.replace(tzinfo=tz, fold=0)
    if first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != local or first.utcoffset() != local.replace(tzinfo=tz, fold=1).utcoffset():
        raise ValueError('Это время попадает на перевод часов. Уточните расписание у врача и выберите однозначное время.')
    return first.astimezone(timezone.utc).replace(tzinfo=None)


def local_label(value, tz):
    return value.replace(tzinfo=timezone.utc).astimezone(zone(tz)).strftime('%d.%m.%Y %H:%M')


def schedule(form, now=None):
    now = now or datetime.utcnow()
    tz = zone(form.get('timezone', ''))
    try:
        start, end = (date.fromisoformat(form.get(k, '')) for k in ('start_date', 'end_date'))
    except (ValueError, TypeError): raise ValueError('Проверьте даты курса.') from None
    today = now.replace(tzinfo=timezone.utc).astimezone(tz).date()
    if not today - timedelta(days=1) <= start <= today + timedelta(days=180) or not 0 <= (end-start).days <= 179 or end < today:
        raise ValueError('Курс: от 1 до 180 дней. Начало — не раньше вчерашнего дня.')
    times = [x.strip() for x in form.get('times', '').split(',') if x.strip()]
    if not 1 <= len(times) <= 6 or len(set(times)) != len(times) or any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', x) for x in times):
        raise ValueError('Укажите от 1 до 6 разных времён в формате 08:00, 20:00.')
    slots = []
    for day in range((end-start).days+1):
        for item in sorted(times):
            due = utc_local(datetime.combine(start+timedelta(days=day), time.fromisoformat(item)), tz)
            if due >= now.replace(second=0, microsecond=0): slots.append(due)
    if not slots: raise ValueError('Все выбранные приёмы уже прошли. Проверьте дату и время.')
    return dict(timezone=tz.key, times=sorted(times), start_date=start, end_date=end), slots


def mark_dose(db, did, uid, state, now=None):
    if state not in ('taken', 'skipped'): raise ValueError('Выберите отметку о приёме.')
    now = now or datetime.utcnow()
    dose = db.get(MedicationDose, did)
    course = db.get(MedicationCourse, dose.course_id) if dose else None
    if not course or not get_pet(db, course.pet_id, uid, lock=True): return None
    if not course.active or dose.due_at > now: raise ValueError('Этот приём ещё не наступил или курс уже остановлен.')
    changed = db.execute(update(MedicationDose).where(MedicationDose.id == did, MedicationDose.state == 'pending').values(
        state=state, marked_by=uid, marked_at=now), execution_options={'synchronize_session': False})
    if changed.rowcount != 1: raise ValueError('Этот приём уже отмечен. Обновите страницу и проверьте, кто дал лекарство. Повторно не давайте.')
    return course.pet_id


def observation(form, now=None):
    now = now or datetime.utcnow()
    tz = zone(form.get('timezone', ''))
    try: observed = utc_local(datetime.fromisoformat(form.get('observed_at', '')), tz)
    except (ValueError, TypeError): raise ValueError('Проверьте дату, время и часовой пояс наблюдения.') from None
    if not now-timedelta(days=366) <= observed <= now+timedelta(minutes=5):
        raise ValueError('Наблюдение должно быть за прошедший год, не в будущем.')
    values = dict(observed_at=observed, timezone=tz.key)
    for key, allowed in [('appetite', APPETITE), ('activity', ACTIVITY), ('pain', PAIN), ('wound', WOUND)]:
        value = form.get(key, 'not_applicable' if key == 'wound' else '')
        if value not in allowed: raise ValueError('Заполните оценки состояния.')
        values[key] = value
    for key, maximum in [('weight', 300), ('vomiting', 100), ('seizures', 100)]:
        raw = form.get(key, '').strip().replace(',', '.')
        try:
            value = float(raw) if key == 'weight' and raw else int(raw) if raw else None
            if value is not None and (not math.isfinite(value) or value < 0 or value > maximum or key == 'weight' and value == 0): raise ValueError()
        except ValueError: raise ValueError('Проверьте вес и количество эпизодов.') from None
        values[key] = value
    flags = form.getlist('flags')
    if any(flag not in FLAGS for flag in flags): raise ValueError('Неизвестный признак.')
    values['flags'] = sorted(set(flags))
    values['notes'] = form.get('notes', '').strip()
    values['condition'] = form.get('condition', '').strip()
    if len(values['notes']) > 4000 or len(values['condition']) > 160: raise ValueError('Сократите заметку.')
    values['urgency'] = ('urgent' if flags else 'contact' if values['activity'] == 'very_low' or values['pain'] == 'severe'
        or values['wound'] in ('worse', 'discharge', 'open') or values['appetite'] == 'none'
        or (values['vomiting'] or 0) >= 2 or (values['seizures'] or 0) > 0 else 'recorded')
    return values
