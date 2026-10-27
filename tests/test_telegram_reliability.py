from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, Conflict

import storage
import prevention_patch as prevention
import prevention_time_patch as timing
import telegram_reliability as reliability
import telegram_billing as billing
from test_telegram_billing import setup_sbp


def due_event(pet, monkeypatch):
    now = datetime(2030, 10, 25, 11)
    monkeypatch.setattr(timing, 'datetime', SimpleNamespace(now=lambda tz: now))
    prevention._save_event(100, pet['id'], 'vaccination', now.date())
    return timing._due_notifications_at_ten()[0][0]


def retry_now():
    with storage.SessionLocal() as db:
        row = db.scalar(select(reliability.ReminderDelivery))
        row.next_attempt = datetime.utcnow() - timedelta(seconds=1)
        db.commit()


async def test_web_only_accounts_do_not_send_telegram(bot, pet, ctx, monkeypatch):
    due_event(pet, monkeypatch)
    with storage.SessionLocal() as db:
        db.scalar(select(storage.User)).telegram_id = -123456
        db.commit()
    assert timing._due_notifications_at_ten() == []
    await prevention._send_due_notifications(ctx)
    ctx.bot.send_message.assert_not_called()
    with storage.SessionLocal() as db:
        assert not db.scalar(select(prevention.PreventiveEvent)).reminded_0


@pytest.mark.parametrize('error', [BadRequest('Chat not found'), Forbidden('Bot was blocked')])
async def test_permanent_failure_stops_without_claiming_delivery(bot, pet, ctx, monkeypatch, error):
    due_event(pet, monkeypatch)
    ctx.bot.send_message.side_effect = error
    await prevention._send_due_notifications(ctx)
    await prevention._send_due_notifications(ctx)
    assert ctx.bot.send_message.call_count == 1
    with storage.SessionLocal() as db:
        assert db.scalar(select(reliability.ReminderDelivery)).state == 'failed'
        assert not db.scalar(select(prevention.PreventiveEvent)).reminded_0


async def test_network_retry_has_delay_limit_and_no_false_success(bot, pet, ctx, monkeypatch):
    due_event(pet, monkeypatch)
    ctx.bot.send_message.side_effect = NetworkError('connection lost')
    await prevention._send_due_notifications(ctx)
    await prevention._send_due_notifications(ctx)
    assert ctx.bot.send_message.call_count == 1
    for _ in range(6):
        retry_now()
        await prevention._send_due_notifications(ctx)
    assert ctx.bot.send_message.call_count == reliability.MAX_ATTEMPTS
    with storage.SessionLocal() as db:
        row = db.scalar(select(reliability.ReminderDelivery))
        assert row.state == 'failed'
        assert not db.scalar(select(prevention.PreventiveEvent)).reminded_0


async def test_retry_after_is_respected(bot, pet, ctx, monkeypatch):
    due_event(pet, monkeypatch)
    ctx.bot.send_message.side_effect = RetryAfter(7200)
    await prevention._send_due_notifications(ctx)
    with storage.SessionLocal() as db:
        row = db.scalar(select(reliability.ReminderDelivery))
        assert row.state == 'retry'
        assert row.next_attempt > datetime.utcnow() + timedelta(seconds=7190)


def test_claim_lease_prevents_duplicate_workers(bot, pet, monkeypatch):
    event_id = due_event(pet, monkeypatch)
    assert reliability.claim_reminder(event_id, 'reminded_0')
    assert not reliability.claim_reminder(event_id, 'reminded_0')
    retry_now()
    assert reliability.claim_reminder(event_id, 'reminded_0')


def test_error_logs_do_not_contain_payload_or_token(caplog):
    secret = '123456:secret-token user medical history'
    reliability.log_error(NetworkError(secret), 'polling')
    assert secret not in caplog.text
    assert 'telegram_network_error' in caplog.text
    assert reliability.error_code(Conflict(secret)).startswith('polling_conflict')


async def test_saved_answer_recovery_is_private_free_and_does_not_call_ai(bot, update, ctx):
    setup_sbp()
    ai = SimpleNamespace(responses=SimpleNamespace(create=Mock(
        return_value=SimpleNamespace(output_text='Saved answer', model='test'))))
    request = update('Question'); request.message.message_id = 701
    await billing.generate(request, ai, model='test', input=[])
    uid = billing.account_id(100)
    for _ in range(2):
        await billing.last_answer(update('/lastanswer'), ctx)
    with storage.SessionLocal() as db:
        assert billing.b.summary(db, uid)['balance'] == 4
    assert ai.responses.create.call_count == 1
    own = update('/lastanswer')
    await billing.last_answer(own, ctx)
    assert 'Saved answer' in own.message.reply_text.call_args.args[0]
    other = update('/lastanswer', user_id=200)
    await billing.last_answer(other, ctx)
    assert 'Saved answer' not in other.message.reply_text.call_args.args[0]
    group = update('/lastanswer'); group.effective_chat.type = 'group'
    await billing.last_answer(group, ctx)
    assert 'Saved answer' not in group.message.reply_text.call_args.args[0]


async def test_delivery_failure_can_recover_saved_answer_without_debit(bot, update, ctx, raw):
    setup_sbp()
    request = update('Что такое УЗИ?'); request.message.message_id = 702
    ctx.user_data['dialog_scope'] = 'general'
    request.message.reply_text.side_effect = NetworkError('offline')
    await bot.message(request, ctx)
    recovery = update('/lastanswer')
    await billing.last_answer(recovery, ctx)
    assert 'Тестовый ответ' in recovery.message.reply_text.call_args.args[0]
    with storage.SessionLocal() as db:
        assert billing.b.summary(db, billing.account_id(100))['balance'] == 4
