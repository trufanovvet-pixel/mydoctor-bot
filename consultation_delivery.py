"""Durable delivery of web consultation requests through the existing Telegram bot."""
import asyncio
import io
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, Integer, String, select, update
from sqlalchemy.orm import Mapped, mapped_column
import storage
from owner_profile import ConsultationContact
from consultation_chat import deliver_message_notifications


class WebConsultationDelivery(storage.Base):
    __tablename__ = 'web_consultation_deliveries'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consultation_id: Mapped[int] = mapped_column(ForeignKey('consultations.id'), unique=True)
    request_key: Mapped[str] = mapped_column(String(100), unique=True)
    state: Mapped[str] = mapped_column(String(20), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def claim_next():
    now = datetime.utcnow()
    with storage.SessionLocal() as db:
        item = db.scalar(select(WebConsultationDelivery).where(
            WebConsultationDelivery.state != 'delivered',
            WebConsultationDelivery.next_attempt <= now,
        ).order_by(WebConsultationDelivery.id).limit(1))
        if item is None:
            return None
        result = db.execute(update(WebConsultationDelivery).where(
            WebConsultationDelivery.id == item.id,
            WebConsultationDelivery.state != 'delivered',
            WebConsultationDelivery.next_attempt <= now,
        ).values(state='sending', attempts=WebConsultationDelivery.attempts + 1,
                 next_attempt=now + timedelta(minutes=2)), execution_options={'synchronize_session': False})
        if result.rowcount != 1:
            db.rollback()
            return None
        request = db.get(storage.Consultation, item.consultation_id)
        payload = (item.id, request.id, request.user_text)
        db.commit()
        return payload


def mark_delivery(delivery_id, delivered):
    with storage.SessionLocal() as db:
        item = db.get(WebConsultationDelivery, delivery_id)
        item.state = 'delivered' if delivered else 'retry'
        item.delivered_at = datetime.utcnow() if delivered else None
        item.next_attempt = datetime.utcnow() + timedelta(seconds=min(300, 30 * item.attempts))
        record = db.get(storage.Consultation, item.consultation_id)
        record.assistant_text = ('Заявка передана врачу. Продолжайте общение в переписке на этой странице. '
                                 'Время консультации и оплата ещё не согласованы.' if delivered else
                                 'Заявка сохранена. Доставка врачу задерживается; отправка повторится автоматически.')
        db.commit()


async def deliver_pending(application):
    from notification_patch import _admin_chat_id
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    for _ in range(20):
        item = await asyncio.to_thread(claim_next)
        if item is None:
            break
        delivery_id, request_id, body = item
        try:
            target = await asyncio.to_thread(_admin_chat_id)
            def buttons():
                with storage.SessionLocal() as db:
                    contacts=db.get(ConsultationContact,request_id)
                    rows=[[InlineKeyboardButton(label,url=url)] for label,url in (contacts.links if contacts else {}).items()]
                    base=os.getenv('MYDOCTOR_WEB_URL','https://mydoctor-web-production.up.railway.app').rstrip('/')
                    rows.append([InlineKeyboardButton('Открыть заявку в кабинете врача',url=base+'/doctor/requests/'+str(request_id))])
                    return InlineKeyboardMarkup(rows)
            markup=await asyncio.to_thread(buttons)
            text = f'Новая заявка с сайта МойДоктор №{request_id}\n\n{body}'
            if len(text.encode('utf-16-le')) // 2 > 3500:
                document = io.BytesIO(text.encode('utf-8'))
                document.name = f'consultation-{request_id}.txt'
                await application.bot.send_document(chat_id=target, document=document,
                    caption=f'Новая заявка с сайта МойДоктор №{request_id}. Полная анкета в файле.',
                    reply_markup=markup,
                    read_timeout=20, write_timeout=20, connect_timeout=10)
            else:
                await application.bot.send_message(chat_id=target, text=text,
                    reply_markup=markup,
                    read_timeout=20, write_timeout=20, connect_timeout=10)
        except Exception as exc:
            logging.getLogger(__name__).warning('Web request %s delivery failed: %s', request_id, type(exc).__name__)
            await asyncio.to_thread(mark_delivery, delivery_id, False)
        else:
            await asyncio.to_thread(mark_delivery, delivery_id, True)


async def delivery_loop(application):
    while True:
        try:
            await deliver_pending(application)
            await deliver_message_notifications(application)
        except Exception as exc:
            logging.getLogger(__name__).error('Web request delivery worker: %s', type(exc).__name__)
        await asyncio.sleep(5)


async def start_delivery_worker(application):
    await asyncio.to_thread(storage.Base.metadata.create_all, storage.engine)
    await asyncio.to_thread(storage.set_bot_setting,'doctor_bot_username',application.bot.username)
    application.bot_data['web_request_delivery'] = asyncio.create_task(delivery_loop(application))


async def stop_delivery_worker(application):
    task = application.bot_data.pop('web_request_delivery', None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
