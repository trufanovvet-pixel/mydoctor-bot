"""Bounded reminder retries and privacy-safe Telegram error diagnostics."""
import logging
import traceback
from datetime import datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, Integer, String, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, Conflict

import storage

MAX_ATTEMPTS = 5
logger = logging.getLogger(__name__)


class ReminderDelivery(storage.Base):
    __tablename__ = 'preventive_reminder_deliveries'
    event_id: Mapped[int] = mapped_column(ForeignKey('preventive_events.id', ondelete='CASCADE'), primary_key=True)
    field: Mapped[str] = mapped_column(String(20), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)


def error_code(error):
    # Never write raw exceptions: provider messages may contain tokens or user text.
    if isinstance(error, Conflict):
        return 'polling_conflict_check_other_instance_or_webhook'
    if isinstance(error, Forbidden):
        return 'recipient_forbidden'
    if isinstance(error, BadRequest):
        value = str(error).lower()
        for needle, code in [('chat not found', 'chat_not_found'),
                             ('message is too long', 'message_too_long'),
                             ("can't parse entities", 'invalid_format'),
                             ('query is too old', 'expired_callback')]:
            if needle in value:
                return code
        return 'bad_request'
    if isinstance(error, RetryAfter):
        return 'rate_limited'
    if isinstance(error, NetworkError):
        return 'telegram_network_error'
    return type(error).__name__


def log_error(error, operation):
    frames = traceback.extract_tb(error.__traceback__)
    location = ' > '.join(f'{frame.name}:{frame.lineno}' for frame in frames[-5:])
    logger.warning('Telegram operation=%s error=%s location=%s', operation, error_code(error), location or 'polling')


def claim_reminder(event_id, field):
    now = datetime.utcnow()
    with storage.SessionLocal() as db:
        item = db.get(ReminderDelivery, (event_id, field))
        if item is None:
            db.add(ReminderDelivery(event_id=event_id, field=field, next_attempt=now))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()  # Another worker created it; the conditional claim below arbitrates.
        result = db.execute(update(ReminderDelivery).where(
            ReminderDelivery.event_id == event_id, ReminderDelivery.field == field,
            ReminderDelivery.state.notin_(['failed', 'delivered']),
            ReminderDelivery.attempts < MAX_ATTEMPTS,
            ReminderDelivery.next_attempt <= now,
        ).values(state='sending', attempts=ReminderDelivery.attempts + 1,
                 next_attempt=now + timedelta(minutes=5)))
        db.commit()
        return result.rowcount == 1


def finish_reminder(event_id, field, error=None):
    from prevention_patch import PreventiveEvent
    with storage.SessionLocal() as db:
        item = db.get(ReminderDelivery, (event_id, field))
        if item is None:
            return
        if error is None:
            item.state = 'delivered'
            event = db.get(PreventiveEvent, event_id)
            if event is not None:
                setattr(event, field, True)
        else:
            item.error_code = error_code(error)
            permanent = isinstance(error, (BadRequest, Forbidden))
            item.state = 'failed' if permanent or item.attempts >= MAX_ATTEMPTS else 'retry'
            delay = min(3600, 60 * 2 ** (item.attempts - 1))
            if isinstance(error, RetryAfter):
                retry_after = error.retry_after
                seconds = retry_after.total_seconds() if isinstance(retry_after, timedelta) else retry_after
                delay = max(delay, seconds)
            item.next_attempt = datetime.utcnow() + timedelta(seconds=delay)
        db.commit()
