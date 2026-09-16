from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

import prevention_patch


MOSCOW = ZoneInfo("Europe/Moscow")
DEFAULT_REMINDER_HOUR = 10


def _due_notifications_at_ten():
    now = datetime.now(MOSCOW)
    today = now.date()

    # Default behavior: reminders are delivered at/after 10:00 Moscow time.
    # The hourly reminder loop marks each reminder after the first successful send,
    # so it will not repeat later in the day.
    if now.hour < DEFAULT_REMINDER_HOUR:
        return []

    notifications = []
    with prevention_patch.SessionLocal() as session:
        rows = session.execute(
            select(prevention_patch.PreventiveEvent, prevention_patch.Pet, prevention_patch.User)
            .join(prevention_patch.Pet, prevention_patch.Pet.id == prevention_patch.PreventiveEvent.pet_id)
            .join(prevention_patch.User, prevention_patch.User.id == prevention_patch.PreventiveEvent.user_id)
            .where(
                prevention_patch.PreventiveEvent.due_date >= today - timedelta(days=1),
                prevention_patch.PreventiveEvent.due_date <= today + timedelta(days=7),
            )
        ).all()

        for event, pet, user in rows:
            days = (event.due_date - today).days
            field = None
            if days == 7 and not event.reminded_7:
                field = "reminded_7"
            elif days == 1 and not event.reminded_1:
                field = "reminded_1"
            elif days == 0 and not event.reminded_0:
                field = "reminded_0"
            if not field:
                continue

            labels = {
                "vaccination": "вакцинация",
                "deworming": "обработка от гельминтов",
                "ectoparasites": "обработка от блох/клещей",
            }
            when = "через 7 дней" if days == 7 else "завтра" if days == 1 else "сегодня"
            notifications.append(
                (
                    user.telegram_id,
                    f"🔔 Напоминание: у {pet.name} {when} запланирована "
                    f"{labels.get(event.kind, event.kind)} — {event.due_date.strftime('%d.%m.%Y')}."
                )
            )
            setattr(event, field, True)

        session.commit()
    return notifications


def install():
    prevention_patch._due_notifications = _due_notifications_at_ten
