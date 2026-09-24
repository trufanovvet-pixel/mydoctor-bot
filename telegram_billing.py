"""Website checkout links and the shared credit ledger for Telegram AI requests."""
import asyncio
import io
import json
import os
import secrets
from types import SimpleNamespace
from urllib.parse import urlencode

from sqlalchemy import ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

import billing as b
import storage
import web_auth

LABEL = '💳 Оплата и баланс'


class TelegramBillingReply(storage.Base):
    __tablename__ = 'billing_telegram_replies'
    usage_id: Mapped[str] = mapped_column(String(32), ForeignKey('billing_credit_usage.id'), primary_key=True)
    answer: Mapped[str] = mapped_column(Text)


def account_id(telegram_id):
    with storage.SessionLocal() as db:
        uid = db.scalar(select(storage.User.id).where(storage.User.telegram_id == telegram_id))
        if uid is None:
            raise b.BillingError('Аккаунт не найден.', 401)
        return uid


def payment_links(telegram_id):
    token = web_auth.create_token(telegram_id)
    base = os.getenv('MYDOCTOR_WEB_URL', 'https://mydoctor-web-production.up.railway.app').rstrip('/')
    return [(route['name'], base + '/login/' + token + '?' + urlencode({'next': '/billing', 'transfer': key}))
            for key, route in b.CARD_TRANSFERS.items()]


def balance_text(uid):
    with storage.SessionLocal() as db:
        b.lock_user(db, uid)
        b.release_stale(db, uid)
        if b.metered(db, uid):
            b.trial(db, uid)
        db.commit()
        info = b.summary(db, uid)
        balance = f"Доступно: {info['balance']} баллов." if info['metered'] else 'Пока действует тестовый доступ без списаний.'
    return (balance + '\n\nКаждый месяц — 5 бесплатных баллов. Обновление 1-го числа (UTC), без накопления.\n'
            '«Мои питомцы» и «Профилактика» всегда бесплатны, даже при нулевом балансе.\n'
            'Вопрос — 1 балл. Разбор фото или PDF до 5 страниц — 5 баллов.\n'
            'Выберите способ оплаты. Ссылка откроет ваш аккаунт на сайте без регистрации по почте.\n'
            'После перевода нажмите «Я оплатил». Пакет начисляется после проверки поступления.\n'
            'Ссылка личная и действует 10 минут. Не пересылайте её.'
            + ('' if b.live() else '\n\nПриём оплат пока не открыт.'))


async def show_payments(update, context=None, notice=None):
    if getattr(update.effective_chat, 'type', None) != 'private':
        await update.effective_message.reply_text('Оплату и баланс можно открыть в личном чате с ботом: /pay')
        return
    user = update.effective_user
    await asyncio.to_thread(storage.ensure_user, user.id, user.username, user.first_name)
    uid = await asyncio.to_thread(account_id, user.id)
    text = await asyncio.to_thread(balance_text, uid)
    links = await asyncio.to_thread(payment_links, user.id)
    await update.effective_message.reply_text((notice + '\n\n' if notice else '') + text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(name, url=url)] for name, url in links]),
        disable_web_page_preview=True)


def request_identity(update, kind):
    message = update.effective_message
    mid = getattr(message, 'message_id', None)
    chat_id = getattr(update.effective_chat, 'id', update.effective_user.id)
    key = f'tg-{chat_id}-{mid}' if type(mid) is int else 'tg-' + secrets.token_hex(16)
    parts = [kind, getattr(message, 'text', None), getattr(message, 'caption', None)]
    for file in [getattr(message, 'voice', None), getattr(message, 'document', None), *(getattr(message, 'photo', None) or [])]:
        if file:
            parts.append(getattr(file, 'file_unique_id', None) or getattr(file, 'file_id', ''))
    return key, json.dumps(parts, ensure_ascii=False).encode()


def create_response(client, uid, kind, key, fingerprint, kwargs):
    usage, replay = b.reserve(uid, kind, key, fingerprint)
    if replay:
        with storage.SessionLocal() as db:
            saved = db.get(TelegramBillingReply, usage.id)
            if not saved:
                raise b.BillingError('Сохранённый ответ не найден.', 409)
            return SimpleNamespace(output_text=saved.answer, model=usage.model)
    try:
        response = client.responses.create(**kwargs)
        answer = (response.output_text or '').strip()
        if not answer:
            raise ValueError('empty AI response')
        with storage.SessionLocal() as db:
            b.complete(db, uid, usage.id, None, response)
            db.add(TelegramBillingReply(usage_id=usage.id, answer=answer))
            db.commit()
        return response
    except Exception:
        b.fail(uid, usage.id)
        raise


async def generate(update, client, kind='chat', **kwargs):
    user = update.effective_user
    await asyncio.to_thread(storage.ensure_user, user.id, user.username, user.first_name)
    uid = await asyncio.to_thread(account_id, user.id)
    key, fingerprint = request_identity(update, kind)
    try:
        return await asyncio.to_thread(create_response, client, uid, kind, key, fingerprint, kwargs)
    except b.BillingError as error:
        if error.status == 402:
            await show_payments(update, notice='Недостаточно баллов для этого запроса.')
        else:
            await update.effective_message.reply_text(str(error))
        return None


def validate_paid_pdf(telegram_id, data):
    with storage.SessionLocal() as db:
        if not b.metered(db, account_id(telegram_id)):
            return
    from pypdf import PdfReader
    try:
        pdf = PdfReader(io.BytesIO(data))
        if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 5:
            raise ValueError('PDF size')
    except Exception as error:
        raise b.BillingError('Для разбора нужен PDF без пароля, не более 5 страниц.') from error


def install(bot):
    b.init_schema()
    rows = [list(row) for row in bot.MENU.keyboard]
    rows.append([LABEL])
    bot.MENU = ReplyKeyboardMarkup(rows, resize_keyboard=True)
    original_message = bot.message

    async def message(update, context):
        if (getattr(update.effective_message, 'text', None) or '').strip() == LABEL:
            return await show_payments(update, context)
        return await original_message(update, context)

    bot.message = message
    bot.payment_command = show_payments
