import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
import billing as b
import storage
import payment_notifications as pn
from notification_patch import _admin_user_id
from test_billing import owner, enable, order_for, report
from test_web_app import reset_db


@pytest.fixture(autouse=True)
def clean():
    reset_db()


def pending():
    c, uid = owner()
    oid, _ = order_for(c, enable())
    report(c, oid)
    return c, uid, oid


def test_report_enqueues_once_and_delivery_retries():
    c, uid, oid = pending()
    with c.session_transaction() as state:
        csrf = state['billing_csrf']
    assert c.post('/billing/orders/' + oid, data={'billing_csrf': csrf,
        'action': 'report', 'reference': 'repeat'}).status_code == 303
    with storage.SessionLocal() as db:
        assert len(db.scalars(select(pn.PaymentNotice)).all()) == 1
    app = SimpleNamespace(bot=SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock(side_effect=RuntimeError('offline'))))
    with patch('notification_patch._admin_chat_id', return_value=123):
        asyncio.run(pn.deliver_payment_notifications(app))
    with storage.SessionLocal() as db:
        notice = db.get(pn.PaymentNotice, oid)
        assert notice.state == 'retry'
        notice.next_attempt = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    app.bot.send_document = AsyncMock()
    app.bot.send_message = AsyncMock()
    with patch('notification_patch._admin_chat_id', return_value=123):
        asyncio.run(pn.deliver_payment_notifications(app))
        asyncio.run(pn.deliver_payment_notifications(app))
    app.bot.send_document.assert_awaited_once()
    document = app.bot.send_document.call_args.kwargs
    assert document['chat_id'] == 123 and oid in document['caption']
    app.bot.send_message.assert_awaited_once()
    sent = app.bot.send_message.call_args.kwargs
    assert sent['chat_id'] == 123 and '499' in sent['text'] and 'TEST payment 12:00' in sent['text']
    assert sent['reply_markup'].inline_keyboard[0][0].callback_data == 'pay:yes:' + oid


def test_decision_admin_only_and_concurrent_confirm_exactly_once():
    c, uid, oid = pending()
    with pytest.raises(PermissionError):
        pn.decide(oid, 'yes', _admin_user_id() + 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: pn.decide(oid, 'yes', _admin_user_id()), range(4)))
    with storage.SessionLocal() as db:
        assert db.get(b.PaymentOrder, oid).status == 'paid'
        assert len(db.scalars(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:' + oid)).all()) == 1
    with pytest.raises(b.BillingError):
        pn.decide(oid, 'no', _admin_user_id())
    assert pn.claim() is None


def test_reject_does_not_credit_and_cannot_be_confirmed_by_stale_button():
    c, uid, oid = pending()
    pn.decide(oid, 'no', _admin_user_id())
    pn.decide(oid, 'no', _admin_user_id())
    with pytest.raises(b.BillingError):
        pn.decide(oid, 'yes', _admin_user_id())
    with storage.SessionLocal() as db:
        assert db.get(b.PaymentOrder, oid).status == 'rejected'
        assert not db.scalar(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:' + oid))
    assert 'Поступление денег не найдено' in c.get('/billing/orders/' + oid).text


def test_callback_rejects_strangers_and_survives_telegram_edit_failure():
    c, uid, oid = pending()
    query = SimpleNamespace(data='pay:yes:' + oid, answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=_admin_user_id() + 1))
    asyncio.run(pn.payment_callback(update, None))
    query.edit_message_text.assert_not_awaited()
    with storage.SessionLocal() as db:
        assert db.get(b.PaymentOrder, oid).status == 'review'
    update.effective_user.id = _admin_user_id()
    query.edit_message_text = AsyncMock(side_effect=RuntimeError('offline'))
    asyncio.run(pn.payment_callback(update, None))
    asyncio.run(pn.payment_callback(update, None))
    with storage.SessionLocal() as db:
        assert len(db.scalars(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:' + oid)).all()) == 1
