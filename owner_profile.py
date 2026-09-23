"""Private owner contacts and immutable contact snapshots for consultation requests."""
import re
from datetime import datetime
from urllib.parse import urlsplit
from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
import storage


class OwnerProfile(storage.Base):
    __tablename__ = 'owner_profiles'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default='')
    phone: Mapped[str] = mapped_column(String(40), default='')
    email: Mapped[str] = mapped_column(String(255), default='')
    telegram: Mapped[str] = mapped_column(String(32), default='')
    whatsapp: Mapped[str] = mapped_column(String(40), default='')
    instagram: Mapped[str] = mapped_column(String(30), default='')
    vk: Mapped[str] = mapped_column(String(64), default='')
    preferred: Mapped[str] = mapped_column(String(20), default='email')
    language: Mapped[str] = mapped_column(String(2), default='ru')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConsultationContact(storage.Base):
    __tablename__ = 'consultation_contacts'
    consultation_id: Mapped[int] = mapped_column(ForeignKey('consultations.id'), primary_key=True)
    links: Mapped[dict] = mapped_column(JSON, default=dict)
    language: Mapped[str] = mapped_column(String(2), default='ru')


def telegram_username(value):
    value = (value or '').strip()
    value = re.sub(r'^(?:https?://)?(?:t\.me|telegram\.me)/', '', value, flags=re.I)
    value = value.removeprefix('@').rstrip('/')
    if not value:
        return ''
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', value):
        raise ValueError('Укажите Telegram в формате @username или https://t.me/username.')
    return value


def social_username(value, hosts):
    value=value.strip()
    if '/' in value or '://' in value:
        parsed=urlsplit(value if '://' in value else 'https://'+value)
        if parsed.scheme not in ('http','https') or parsed.netloc.lower() not in hosts:
            raise ValueError('Укажите имя пользователя или прямую ссылку на профиль Instagram / ВК.')
        return parsed.path.strip('/')
    return value.removeprefix('@')


def validate_contacts(form):
    values = {key: form.get(key, '').strip() for key in ('name', 'phone', 'email', 'telegram', 'whatsapp', 'instagram', 'vk', 'preferred')}
    if len(values['name']) > 120:
        raise ValueError('Имя должно быть не длиннее 120 символов.')
    for key in ('phone', 'whatsapp'):
        phone = values[key]
        if phone and (not re.fullmatch(r'\+?[0-9 ()\-\.]{7,40}', phone) or not 7 <= len(re.sub(r'\D', '', phone)) <= 15):
            raise ValueError('Проверьте телефон или WhatsApp. Укажите номер с кодом страны, например +66 или +7.')
        if key=='whatsapp' and phone and not phone.startswith('+'):
            raise ValueError('Проверьте телефон или WhatsApp. Укажите номер с кодом страны, например +66 или +7.')
    email = values['email']
    if email and (len(email) > 255 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)):
        raise ValueError('Проверьте адрес электронной почты.')
    values['telegram'] = telegram_username(values['telegram'])
    for key,hosts,pattern in [('instagram',('instagram.com','www.instagram.com'),r'[A-Za-z0-9_.]{1,30}'),('vk',('vk.com','vk.ru','www.vk.com','www.vk.ru'),r'[A-Za-z0-9_.]{2,64}')]:
        value=social_username(values[key],hosts)
        if value and (not re.fullmatch(pattern,value) or '..' in value or value.startswith('.') or value.endswith('.')):
            raise ValueError('Укажите имя пользователя или прямую ссылку на профиль Instagram / ВК.')
        values[key]=value
    if values['preferred'] not in ('phone', 'email', 'telegram', 'whatsapp', 'instagram', 'vk'):
        raise ValueError('Выберите удобный способ связи.')
    if not values[values['preferred']]:
        raise ValueError('Заполните контакт для выбранного способа связи.')
    return values


def profile_values(profile, user=None, email=''):
    if profile:
        return {key: getattr(profile, key) for key in ('name', 'phone', 'email', 'telegram', 'whatsapp', 'instagram', 'vk', 'preferred')}
    return dict(name=user.first_name or '' if user else '', phone='', email=email, telegram='', whatsapp='', instagram='', vk='', preferred='email')


def preferred_contact(values):
    kind = values.get('preferred', 'email')
    value = values.get(kind, '')
    if kind in ('telegram','instagram') and value:return '@' + value
    if kind=='vk' and value:return 'https://vk.com/'+value
    return value


def contact_links(values):
    links={}
    if values.get('telegram'):links['Telegram']='https://t.me/'+values['telegram']
    if values.get('whatsapp'):links['WhatsApp']='https://wa.me/'+re.sub(r'\D','',values['whatsapp'])
    if values.get('instagram'):links['Instagram']='https://www.instagram.com/'+values['instagram']+'/'
    if values.get('vk'):links['VK']='https://vk.com/'+values['vk']
    return links
