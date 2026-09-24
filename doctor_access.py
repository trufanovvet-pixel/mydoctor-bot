"""Doctor access is granted only by the existing administrator's Telegram account."""
import asyncio
import hashlib
import os
import re
import secrets
from urllib.parse import urlencode
from datetime import datetime, timedelta
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, delete, func, select, update
from sqlalchemy.orm import Mapped, mapped_column
import storage

DOCTOR_COOKIE = '__Host-mydoctor-doctor'
REMEMBER_SECONDS = 30 * 24 * 60 * 60


def doctor_destination(value):
    """Only supported doctor pages may be used as a login return address."""
    return value if isinstance(value,str) and re.fullmatch(r'/doctor(?:/|/dashboard|/account|/ai|/requests/[1-9]\d*|/clients(?:/[1-9]\d*)?|/payments(?:/[a-f0-9]{32})?|/install)?',value) else '/doctor'


class DoctorWebAccount(storage.Base):
    """An existing web account explicitly linked after administrator authentication."""
    __tablename__ = 'doctor_web_accounts'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DoctorLoginAttempt(storage.Base):
    __tablename__ = 'doctor_login_attempts'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


def allow_doctor_password_attempt(email):
    """Shared by workers, independent of cookies; never store passwords or raw email."""
    cutoff = datetime.utcnow() - timedelta(minutes=15)
    subject = hashed(email.strip().lower())
    with storage.SessionLocal() as db:
        db.execute(delete(DoctorLoginAttempt).where(DoctorLoginAttempt.created_at < cutoff))
        count = db.scalar(select(func.count()).select_from(DoctorLoginAttempt).where(DoctorLoginAttempt.subject == subject))
        if count >= 10:
            db.commit()
            return False
        db.add(DoctorLoginAttempt(subject=subject))
        db.commit()
        return True


def revoke_current_doctor_session():
    from flask import request, session
    grants = {value for value in (request.cookies.get(DOCTOR_COOKIE), session.get('doctor_grant')) if value}
    if grants:
        with storage.SessionLocal() as db:
            db.execute(update(DoctorAccess).where(DoctorAccess.session_digest.in_([hashed(value) for value in grants]))
                       .values(session_expires_at=datetime.utcnow()))
            db.commit()
    session.pop('doctor_grant', None)
    # Keep this browser's CSRF token: switching the owner account must revoke
    # doctor authority, but must not break an already open doctor login form.


def account_doctor_identity(uid):
    if not uid:
        return None
    from notification_patch import _admin_user_id
    admin = _admin_user_id()
    with storage.SessionLocal() as db:
        user = db.get(storage.User, uid)
        binding = db.get(DoctorWebAccount, uid)
        # Telegram IDs are server-owned; email/name/phone are never sufficient proof.
        if user and (user.telegram_id == admin or (binding and binding.telegram_id == admin)):
            return admin
    return None


def establish_doctor_session(uid, remember=False):
    """Issue the same revocable grant as Telegram, only after password verification."""
    from flask import session
    actor = account_doctor_identity(uid)
    if not actor:
        return False
    token = create_doctor_link(actor)
    session['doctor_grant'] = consume_doctor_link(token, remember=remember)
    session['doctor_csrf'] = secrets.token_urlsafe(32)
    return True


def telegram_login_url(target):
    username=storage.get_bot_setting('doctor_bot_username')
    if not username or not re.fullmatch(r'[A-Za-z0-9_]{5,32}',username):return None
    target=doctor_destination(target)
    payload='doctor_'+target.rsplit('/',1)[1] if target.startswith('/doctor/requests/') else 'doctor_install' if target=='/doctor/install' else 'doctor'
    if target=='/doctor/payments':payload='doctor_payments'
    elif target=='/doctor/account':payload='doctor_account'
    elif target.startswith('/doctor/payments/'):payload='doctor_payment_'+target.rsplit('/',1)[1]
    return f'https://t.me/{username}?start={payload}'


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


def consume_doctor_link(token,remember=False):
    now=datetime.utcnow();grant=secrets.token_urlsafe(32)
    with storage.SessionLocal() as db:
        result=db.execute(update(DoctorAccess).where(DoctorAccess.digest==hashed(token),DoctorAccess.used_at.is_(None),DoctorAccess.expires_at>now)
            .values(used_at=now,session_digest=hashed(grant),session_expires_at=now+timedelta(seconds=REMEMBER_SECONDS) if remember else now+timedelta(hours=8)))
        db.commit()
        return grant if result.rowcount==1 else None


def doctor_identity(grant):
    if not grant:return None
    from notification_patch import _admin_user_id
    with storage.SessionLocal() as db:
        return db.scalar(select(DoctorAccess.telegram_id).where(DoctorAccess.session_digest==hashed(grant),DoctorAccess.telegram_id==_admin_user_id(),DoctorAccess.used_at.is_not(None),DoctorAccess.session_expires_at>datetime.utcnow()))


async def doctor_command(update,context,target='/doctor'):
    from notification_patch import _admin_user_id
    if not update.effective_user or update.effective_user.id!=_admin_user_id():
        await update.message.reply_text('Кабинет врача доступен только администратору.')
        return
    if not update.effective_chat or update.effective_chat.type!='private':
        await update.message.reply_text('Для входа отправьте /doctor в личный чат с ботом.')
        return
    target=doctor_destination(target)
    if target.startswith('/doctor/requests/'):
        def exists():
            with storage.SessionLocal() as db:
                return db.scalar(select(storage.Consultation.id).where(storage.Consultation.id==int(target.rsplit('/',1)[1]),storage.Consultation.kind=='consult_request')) is not None
        if not await asyncio.to_thread(exists):
            await update.message.reply_text('Заявка не найдена. Откройте кабинет врача командой /doctor.')
            return
    token=await asyncio.to_thread(create_doctor_link,update.effective_user.id)
    base=os.getenv('MYDOCTOR_WEB_URL','https://mydoctor.vet').rstrip('/')
    from telegram import InlineKeyboardButton,InlineKeyboardMarkup
    label='Установить приложение' if target=='/doctor/install' else 'Открыть заявку №'+target.rsplit('/',1)[1] if target.startswith('/doctor/requests/') else 'Открыть кабинет врача'
    await update.message.reply_text('Вход подтверждён через ваш Telegram. Нажмите кнопку ниже.\nНа сайте можно запомнить это устройство на 30 дней. Ссылка действует 10 минут; не пересылайте её.',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(label,url=base+'/doctor/access/'+token+'?'+urlencode({'next':target}))]]))


def doctor_start_handler(normal_start):
    async def start(update,context):
        args=getattr(context,'args',None) or []
        payload=args[0] if args else ''
        if payload=='doctor':
            return await doctor_command(update,context)
        if payload=='doctor_install':
            return await doctor_command(update,context,'/doctor/install')
        if payload=='doctor_account':
            return await doctor_command(update,context,'/doctor/account')
        if payload=='doctor_payments':
            return await doctor_command(update,context,'/doctor/payments')
        payment=re.fullmatch(r'doctor_payment_([a-f0-9]{32})',payload)
        if payment:
            return await doctor_command(update,context,'/doctor/payments/'+payment.group(1))
        match=re.fullmatch(r'doctor_([1-9]\d{0,18})',payload)
        if match:
            return await doctor_command(update,context,'/doctor/requests/'+match.group(1))
        return await normal_start(update,context)
    return start
