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
    app = SimpleNamespace(bot=SimpleNamespace(send_document=AsyncMock(side_effect=RuntimeError('offline')), send_message=AsyncMock()))
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
    assert document['chat_id'] == 123 and '500' in document['caption'] and 'TEST payment 12:00' in document['caption']
    assert document['reply_markup'].inline_keyboard[0][0].callback_data == 'pay:yes:' + oid
    app.bot.send_message.assert_not_awaited()


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


def callback_update(oid, action='yes', text=None, actor=None):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    query = SimpleNamespace(data=f'pay:{action}:{oid}', message=message,
        answer=AsyncMock(), edit_message_text=AsyncMock(),
        edit_message_caption=AsyncMock(), edit_message_reply_markup=AsyncMock())
    return SimpleNamespace(callback_query=query,
        effective_user=SimpleNamespace(id=_admin_user_id() if actor is None else actor))


def test_callback_rejects_strangers_and_survives_telegram_edit_failure():
    c, uid, oid = pending()
    update = callback_update(oid, actor=_admin_user_id() + 1)
    query = update.callback_query
    asyncio.run(pn.payment_callback(update, None))
    query.edit_message_text.assert_not_awaited()
    query.edit_message_caption.assert_not_awaited()
    query.message.reply_text.assert_not_awaited()
    with storage.SessionLocal() as db:
        assert db.get(b.PaymentOrder, oid).status == 'review'
    update.effective_user.id = _admin_user_id()
    query.edit_message_caption = AsyncMock(side_effect=RuntimeError('offline'))
    asyncio.run(pn.payment_callback(update, None))
    asyncio.run(pn.payment_callback(update, None))
    assert query.message.reply_text.await_count == 2
    assert query.edit_message_reply_markup.await_count == 2
    with storage.SessionLocal() as db:
        assert len(db.scalars(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:' + oid)).all()) == 1


@pytest.mark.parametrize('text', [None, 'Original text payment notice'])
def test_callback_reports_committed_access_in_same_chat_and_updates_receipt_or_text(text):
    c, uid, oid = pending()
    update = callback_update(oid, text=text)
    query = update.callback_query
    observations = []

    async def receive_reply(message, **kwargs):
        with storage.SessionLocal() as db:
            grant = db.scalar(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:' + oid))
            observations.append((db.get(b.PaymentOrder, oid).status,
                                 b.has_paid_access(db, uid), grant.remaining if grant else None))

    query.message.reply_text.side_effect = receive_reply
    asyncio.run(pn.payment_callback(update, None))
    query.message.reply_text.assert_awaited_once()
    assert observations == [('paid', True, 20)]
    message = query.message.reply_text.call_args.args[0]
    kwargs = query.message.reply_text.call_args.kwargs
    assert message.startswith('✅ Оплата подтверждена.')
    assert 'предоставлен доступ' in message
    assert 'QA' in message and 'Старт, 20 баллов' in message and oid in message
    assert kwargs['parse_mode'] is None
    assert kwargs['do_quote'] is True
    assert kwargs['disable_notification'] is False
    edited, unused = ((query.edit_message_caption, query.edit_message_text) if text is None
                     else (query.edit_message_text, query.edit_message_caption))
    edited.assert_awaited_once()
    assert edited.call_args.kwargs['reply_markup'] is None
    unused.assert_not_awaited()


def test_callback_rejection_has_separate_feedback_without_grant():
    c, uid, oid = pending()
    update = callback_update(oid, action='no')
    asyncio.run(pn.payment_callback(update, None))
    reply = update.callback_query.message.reply_text.call_args.args[0]
    assert reply.startswith('❌ Оплата не подтверждена.')
    with storage.SessionLocal() as db:
        assert db.get(b.PaymentOrder, oid).status == 'rejected'
        assert not db.scalar(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:' + oid))


def test_callback_decision_error_does_not_claim_access_or_remove_buttons():
    c, uid, oid = pending()
    pn.decide(oid, 'no', _admin_user_id())
    update = callback_update(oid)
    asyncio.run(pn.payment_callback(update, None))
    query = update.callback_query
    assert query.message.reply_text.call_args.args[0].startswith('⚠️')
    assert 'предоставлен доступ' not in query.message.reply_text.call_args.args[0]
    query.edit_message_caption.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()
    query.edit_message_reply_markup.assert_not_awaited()


def test_callback_database_failure_gives_retry_feedback_without_success():
    c, uid, oid = pending()
    update = callback_update(oid)
    with patch.object(pn, 'decide', side_effect=RuntimeError('unavailable')):
        asyncio.run(pn.payment_callback(update, None))
    query = update.callback_query
    reply = query.message.reply_text.call_args.args[0]
    assert reply.startswith('⚠️ Не удалось проверить результат операции.')
    assert 'нажмите кнопку повторно' in reply
    query.edit_message_caption.assert_not_awaited()
    with storage.SessionLocal() as db:
        assert db.get(b.PaymentOrder, oid).status == 'review'


def test_callback_reply_failure_still_updates_receipt_and_alerts():
    c, uid, oid = pending()
    update = callback_update(oid)
    query = update.callback_query
    query.message.reply_text.side_effect = RuntimeError('offline')
    asyncio.run(pn.payment_callback(update, None))
    query.edit_message_caption.assert_awaited_once()
    assert query.answer.call_args.kwargs['show_alert'] is True
    assert '✅ Оплата подтверждена.' in query.answer.call_args.args[0]
    with storage.SessionLocal() as db:
        assert db.get(b.PaymentOrder, oid).status == 'paid'


def test_callback_expired_ack_does_not_block_valid_payment():
    c, uid, oid = pending()
    update = callback_update(oid)
    query = update.callback_query
    query.answer.side_effect = RuntimeError('query is too old')
    asyncio.run(pn.payment_callback(update, None))
    query.message.reply_text.assert_awaited_once()
    query.edit_message_caption.assert_awaited_once()
    with storage.SessionLocal() as db:
        assert db.get(b.PaymentOrder, oid).status == 'paid'


def test_callback_invalid_payload_does_not_process_payment():
    c, uid, oid = pending()
    update = callback_update(oid)
    update.callback_query.data = 'pay:yes:not-an-order-id'
    asyncio.run(pn.payment_callback(update, None))
    update.callback_query.message.reply_text.assert_not_awaited()
    with storage.SessionLocal() as db:
        assert db.get(b.PaymentOrder, oid).status == 'review'
