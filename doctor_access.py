"""Doctor access is granted only by the existing administrator's Telegram account."""
import asyncio
import hashlib
import os
import secrets
from datetime import datetime, timedelta
from sqlalchemy import BigInteger, DateTime, String, select, update
from sqlalchemy.orm import Mapped, mapped_column
import storage


class DoctorAccess(storage.Base):
    __tablename__='doctor_access_links'
    digest: Mapped[str]=mapped_column(String(64),primary_key=True)
    telegram_id: Mapped[int]=mapped_column(BigInteger)
    expires_at: Mapped[datetime]=mapped_column(DateTime)
    used_at: Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    session_digest: Mapped[str|None]=mapped_column(String(64),nullable=True,unique=True)
    session_expires_at: Mapped[datetime|None]=mapped_column(DateTime,nullable=True)


def hashed(value):return hashlib.sha256(value.encode()).hexdigest()


def create_doctor_link(telegram_id):
    from notification_patch import _admin_user_id
    if telegram_id!=_admin_user_id():raise PermissionError('Administrator required')
    storage.Base.metadata.create_all(storage.engine)
    token=secrets.token_urlsafe(32)
    with storage.SessionLocal() as db:
        db.add(DoctorAccess(digest=hashed(token),telegram_id=telegram_id,expires_at=datetime.utcnow()+timedelta(minutes=10)))
        db.commit()
    return token


def consume_doctor_link(token):
    now=datetime.utcnow();grant=secrets.token_urlsafe(32)
    with storage.SessionLocal() as db:
        result=db.execute(update(DoctorAccess).where(DoctorAccess.digest==hashed(token),DoctorAccess.used_at.is_(None),DoctorAccess.expires_at>now)
            .values(used_at=now,session_digest=hashed(grant),session_expires_at=now+timedelta(hours=8)))
        db.commit()
        return grant if result.rowcount==1 else None


def doctor_identity(grant):
    if not grant:return None
    with storage.SessionLocal() as db:
        return db.scalar(select(DoctorAccess.telegram_id).where(DoctorAccess.session_digest==hashed(grant),DoctorAccess.used_at.is_not(None),DoctorAccess.session_expires_at>datetime.utcnow()))


async def doctor_command(update,context):
    from notification_patch import _admin_user_id
    if not update.effective_user or update.effective_user.id!=_admin_user_id():
        await update.message.reply_text('Кабинет врача доступен только администратору.')
        return
    if not update.effective_chat or update.effective_chat.type!='private':
        await update.message.reply_text('Для входа отправьте /doctor в личный чат с ботом.')
        return
    token=await asyncio.to_thread(create_doctor_link,update.effective_user.id)
    base=os.getenv('MYDOCTOR_WEB_URL','https://mydoctor-web-production.up.railway.app').rstrip('/')
    from telegram import InlineKeyboardButton,InlineKeyboardMarkup
    await update.message.reply_text('Ваш кабинет врача: заявки, документы, контакты и статусы.\nСсылка одноразовая, действует 10 минут. Не пересылайте её.',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Открыть кабинет врача',url=base+'/doctor/access/'+token)]]))
