"""Durable Telegram payment notices and administrator-only decisions."""
import asyncio
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, Integer, String, select, update
from sqlalchemy.orm import Mapped, mapped_column
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import billing as b
import storage


class PaymentNotice(storage.Base):
    __tablename__ = 'payment_notices'
    order_id: Mapped[str] = mapped_column(ForeignKey('billing_payment_orders.id'), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


def claim():
    now = datetime.utcnow()
    with storage.SessionLocal() as db:
        notice = db.scalar(select(PaymentNotice).where(PaymentNotice.state != 'done',
            PaymentNotice.next_attempt <= now).order_by(PaymentNotice.next_attempt).limit(1))
        if not notice:
            return None
        result = db.execute(update(PaymentNotice).where(PaymentNotice.order_id == notice.order_id,
            PaymentNotice.state != 'done', PaymentNotice.next_attempt <= now).values(
            state='sending', attempts=PaymentNotice.attempts + 1,
            next_attempt=now + timedelta(minutes=2)), execution_options={'synchronize_session': False})
        if result.rowcount != 1:
            db.rollback()
            return None
        order = db.get(b.PaymentOrder, notice.order_id)
        if order.status != 'review':
            notice.state = 'done'
            db.commit()
            return None
        user = db.get(storage.User, order.user_id)
        name = (user.first_name or user.username or 'Владелец')[:255]
        body = (f'Проверка оплаты №{order.id}\nВладелец: {name} (ID {order.user_id})\n'
                f'Пакет: {b.PLANS[order.plan]["name"]}\n'
                f'Сумма: {b.money(order.amount_minor, order.currency)}\n'
                f'Способ: {order.method_name}\n'
                f'Данные от владельца: {order.payment_reference or "—"}\n\n'
                'Сначала проверьте поступление денег в банковском приложении. '
                'Кнопка «Деньги получены» сразу начислит пакет. Сообщение владельца не подтверждает перевод.')
        oid = order.id
        db.commit()
        return oid, body


def mark(oid, success):
    with storage.SessionLocal() as db:
        notice = db.get(PaymentNotice, oid)
        notice.state = 'done' if success else 'retry'
        notice.next_attempt = datetime.utcnow() + timedelta(seconds=60)
        db.commit()


async def deliver_payment_notifications(application):
    from notification_patch import _admin_chat_id
    for _ in range(20):
        item = await asyncio.to_thread(claim)
        if item is None:
            break
        oid, body = item
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton('Деньги получены — подтвердить', callback_data='pay:yes:' + oid)], [
            InlineKeyboardButton('Не поступило', callback_data='pay:no:' + oid)]])
        try:
            target = await asyncio.to_thread(_admin_chat_id)
            await application.bot.send_message(chat_id=target, text=body, reply_markup=buttons,
                parse_mode=None, read_timeout=20, write_timeout=20, connect_timeout=10)
        except Exception as exc:
            logging.getLogger(__name__).warning('Payment notice delivery failed: %s', type(exc).__name__)
            await asyncio.to_thread(mark, oid, False)
        else:
            await asyncio.to_thread(mark, oid, True)


def decide(oid, action, actor):
    from notification_patch import _admin_user_id
    if actor != _admin_user_id():
        raise PermissionError('Administrator required')
    if action not in ('yes', 'no'):
        raise b.BillingError('Неизвестное действие.')
    with storage.SessionLocal() as db:
        order = db.get(b.PaymentOrder, oid)
        if not order:
            raise b.BillingError('Заявка не найдена.', 404)
        uid = order.user_id
    if action == 'yes':
        b.confirm_order(uid, oid, actor)
        return 'Оплата подтверждена. Пакет начислен один раз.'
    with storage.SessionLocal() as db:
        b.lock_user(db, uid)
        order = db.get(b.PaymentOrder, oid)
        if order.status == 'rejected':
            return 'Оплата уже отмечена как не поступившая.'
        if order.status != 'review':
            raise b.BillingError('Заявка уже обработана. Статус: ' + b.ORDER_STATES[order.status], 409)
        order.status = 'rejected'
        order.decision_note = 'Поступление денег не найдено. Проверьте перевод и свяжитесь с поддержкой.'
        order.checked_by = str(actor)
        db.commit()
    return 'Оплата не подтверждена. Пакет не начислен; владелец видит статус на сайте.'


async def payment_callback(update, context):
    from notification_patch import _admin_user_id
    query = update.callback_query
    if not update.effective_user or update.effective_user.id != _admin_user_id():
        await query.answer('Подтверждать оплаты может только врач-администратор.', show_alert=True)
        return
    match = re.fullmatch(r'pay:(yes|no):([a-f0-9]{32})', query.data or '')
    if not match:
        await query.answer('Некорректная заявка.', show_alert=True)
        return
    await query.answer()
    try:
        result = await asyncio.to_thread(decide, match[2], match[1], update.effective_user.id)
    except b.BillingError as exc:
        result = str(exc)
    # Commit before editing Telegram: delivery failures cannot duplicate credits.
    try:
        await query.edit_message_text(f'Оплата №{match[2]}\n{result}', parse_mode=None)
    except Exception as exc:
        logging.getLogger(__name__).warning('Payment decision message update failed: %s', type(exc).__name__)
