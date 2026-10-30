from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from sqlalchemy import select
from telegram.error import RetryAfter, Forbidden, TimedOut
from test_pet_care import account, due_course, post, clean
from care_models import CareNotice, CareNoticeAttempt
import storage
import care_notifications as notices


def ready():
    client, uid, pid = account()
    due_course(uid, pid)
    with storage.SessionLocal() as db:
        db.get(storage.User, uid).telegram_id = 11229933
        db.commit()
    post(client, f'/care/pets/{pid}/preferences', {'reminders':'1'})
    return client, uid, pid


@pytest.mark.asyncio
async def test_telegram_retry_after_then_success_is_recorded_once():
    ready()
    app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=RetryAfter(120))))
    await notices.deliver_care_notifications(app)
    with storage.SessionLocal() as db:
        row = db.scalar(select(CareNotice))
        assert row.state == 'retry'
        assert row.next_attempt > datetime.utcnow() + timedelta(seconds=110)
        row.next_attempt = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    app.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=12345))
    await notices.deliver_care_notifications(app)
    await notices.deliver_care_notifications(app)
    assert app.bot.send_message.await_count == 1
    with storage.SessionLocal() as db:
        attempts = db.scalars(select(CareNoticeAttempt).order_by(CareNoticeAttempt.id)).all()
        assert [a.outcome for a in attempts] == ['retry', 'delivered']
        assert attempts[-1].message_id == '12345'


@pytest.mark.asyncio
@pytest.mark.parametrize('error,state', [(Forbidden('blocked'), 'blocked'), (TimedOut(), 'uncertain')])
async def test_blocked_and_ambiguous_delivery_do_not_repeat(error,state):
    client, uid, pid = ready()
    app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=error)))
    await notices.deliver_care_notifications(app)
    await notices.deliver_care_notifications(app)
    assert app.bot.send_message.await_count == 1
    with storage.SessionLocal() as db:
        assert db.scalar(select(CareNotice)).state == state
    assert client.get(f'/care/pets/{pid}').status_code == 200


def test_expired_inflight_is_not_blindly_resent():
    ready()
    notices.enqueue_due()
    nid = notices.claim_notice()
    with storage.SessionLocal() as db:
        db.get(CareNotice,nid).next_attempt = datetime.utcnow()-timedelta(seconds=1)
        db.commit()
    assert notices.claim_notice() is None
    with storage.SessionLocal() as db:
        assert db.get(CareNotice,nid).state == 'uncertain'


@pytest.mark.asyncio
async def test_push_success_does_not_cancel_telegram_retry():
    from web_push import PushSubscription
    client, uid, pid = ready()
    with storage.SessionLocal() as db:
        db.add(PushSubscription(id='t'*64,role='owner',subject_id=uid,
            endpoint='https://fcm.googleapis.com/fcm/send/test',p256dh='key',auth='auth',
            binding='test',expires_at=datetime.utcnow()+timedelta(days=1)))
        db.commit()
    app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=RetryAfter(1))))
    with patch.object(notices, 'send_push', return_value='delivered') as push:
        await notices.deliver_care_notifications(app)
        with storage.SessionLocal() as db:
            rows={r.channel:r for r in db.scalars(select(CareNotice)).all()}
            assert rows['push'].state == 'delivered'
            assert rows['telegram'].state == 'retry'
            rows['telegram'].next_attempt=datetime.utcnow()-timedelta(seconds=1)
            db.commit()
        app.bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=99))
        await notices.deliver_care_notifications(app)
        await notices.deliver_care_notifications(app)
        assert push.call_count == 1
        assert app.bot.send_message.await_count == 1


def test_verified_doctor_web_account_is_valid_telegram_destination():
    from doctor_access import DoctorWebAccount
    from notification_patch import _admin_user_id
    client, uid, pid = account()
    with storage.SessionLocal() as db:
        assert notices.telegram_destination(db, uid) is None
        db.add(DoctorWebAccount(user_id=uid, telegram_id=_admin_user_id()))
        db.commit()
        assert notices.telegram_destination(db, uid) == _admin_user_id()
        db.delete(db.get(DoctorWebAccount, uid))
        db.commit()
        assert notices.telegram_destination(db, uid) is None
