"""Opt-in Web Push, bound to an authenticated browser and delivered from an outbox."""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from flask import abort, g, jsonify, request, session
from pywebpush import WebPushException, webpush
from requests import Session
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

import storage


class PushSubscription(storage.Base):
    __tablename__ = 'web_push_subscriptions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(10), index=True)
    subject_id: Mapped[int] = mapped_column(BigInteger, index=True)
    endpoint: Mapped[str] = mapped_column(Text)
    p256dh: Mapped[str] = mapped_column(String(100))
    auth: Mapped[str] = mapped_column(String(30))
    binding: Mapped[str] = mapped_column(String(64), index=True)
    doctor_grant: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language: Mapped[str] = mapped_column(String(2), default='ru')
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class PushDelivery(storage.Base):
    __tablename__ = 'web_push_deliveries'
    __table_args__ = (UniqueConstraint('subscription_id', 'message_id'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[str] = mapped_column(ForeignKey('web_push_subscriptions.id'), index=True)
    binding: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[int] = mapped_column(ForeignKey('consultation_messages.id'))
    state: Mapped[str] = mapped_column(String(20), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def vapid_keys():
    """Generate once in the shared DB, including when web workers start concurrently."""
    key_name = 'web_push_vapid_v1'
    with storage.SessionLocal() as db:
        row = db.get(storage.BotSetting, key_name)
        if row: return json.loads(row.value)
        key = ec.generate_private_key(ec.SECP256R1())
        keys = dict(private=b64(key.private_numbers().private_value.to_bytes(32, 'big')),
                    public=b64(key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)))
        db.add(storage.BotSetting(key=key_name, value=json.dumps(keys)))
        try:
            db.commit()
            return keys
        except IntegrityError:
            db.rollback()
            return json.loads(db.get(storage.BotSetting, key_name).value)


def valid_endpoint(endpoint):
    if not isinstance(endpoint, str) or len(endpoint) > 2048 or any(ord(c) <= 32 for c in endpoint): return False
    try:
        url = urlsplit(endpoint)
        host = url.hostname or ''
        allowed = host in ('fcm.googleapis.com', 'web.push.apple.com', 'updates.push.services.mozilla.com') or host.endswith('.notify.windows.com')
        return bool(allowed and url.scheme == 'https' and url.port in (None, 443) and
                    not url.username and not url.password and not url.fragment and not url.query and len(url.path) > 1)
    except ValueError:
        return False


def parse_subscription(value):
    if not isinstance(value, dict) or not valid_endpoint(value.get('endpoint')): raise ValueError('endpoint')
    keys = value.get('keys')
    if not isinstance(keys, dict): raise ValueError('keys')
    result = {'endpoint': value['endpoint']}
    for name, length in (('p256dh', 65), ('auth', 16)):
        encoded = keys.get(name, '')
        if not isinstance(encoded, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,90}={0,2}', encoded): raise ValueError('key')
        raw = base64.urlsafe_b64decode(encoded+'='*((-len(encoded)) % 4))
        if len(raw) != length: raise ValueError('key')
        if name == 'p256dh': ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        result[name] = b64(raw)
    return result


def remove_subscription(db, sid):
    db.execute(delete(PushDelivery).where(PushDelivery.subscription_id == sid))
    db.execute(delete(PushSubscription).where(PushSubscription.id == sid))


def revoke_browser_subscriptions(role=None):
    with storage.SessionLocal() as db:
        for item_role in (('owner', 'doctor') if role is None else (role,)):
            token = session.pop('push_'+item_role+'_device', None)
            if token:
                ids = db.scalars(select(PushSubscription.id).where(PushSubscription.binding == digest(token), PushSubscription.role == item_role)).all()
                for sid in ids: remove_subscription(db, sid)
        db.commit()


def install(app):
    storage.Base.metadata.create_all(storage.engine)

    def identity(role):
        if role not in ('owner', 'doctor'): abort(400)
        uid = app.extensions['doctor_identity']() if role == 'doctor' else session.get('uid')
        if not uid: abort(403 if role == 'doctor' else 401)
        if role == 'owner':
            from web_app import WebAccount
            with storage.SessionLocal() as db:
                if not db.scalar(select(WebAccount.id).where(WebAccount.user_id == uid)): abort(401)
        return uid

    @app.after_request
    def push_headers(response):
        if request.path.startswith('/api/push/'):
            response.headers['Cache-Control'] = 'private, no-store'
        return response

    @app.get('/api/push/config')
    def push_config():
        role = request.args.get('role', 'owner'); uid = identity(role)
        session.setdefault('pwa_csrf', secrets.token_urlsafe(32))
        token = session.setdefault('push_'+role+'_device', secrets.token_urlsafe(32))
        with storage.SessionLocal() as db:
            enabled = bool(db.scalar(select(PushSubscription.id).where(PushSubscription.role == role,
                PushSubscription.subject_id == uid, PushSubscription.binding == digest(token),
                PushSubscription.expires_at > datetime.utcnow())))
        return jsonify(public_key=vapid_keys()['public'], csrf=session['pwa_csrf'], enabled=enabled)

    @app.post('/api/push/subscription')
    def push_subscription():
        if request.content_length and request.content_length > 8192: abort(413)
        data = request.get_json(silent=True)
        if not isinstance(data, dict): abort(400)
        role = data.get('role', 'owner'); uid = identity(role)
        expected = session.get('pwa_csrf', '')
        if not expected or not secrets.compare_digest(expected, request.headers.get('X-PWA-CSRF', '')): abort(400)
        token = session.get('push_'+role+'_device')
        if not token: abort(400)
        binding = digest(token)
        with storage.SessionLocal() as db:
            if data.get('action') == 'disable':
                ids = db.scalars(select(PushSubscription.id).where(PushSubscription.role == role,
                    PushSubscription.subject_id == uid, PushSubscription.binding == binding)).all()
                for sid in ids: remove_subscription(db, sid)
                db.commit(); return jsonify(ok=True, enabled=False)
            if data.get('action') != 'enable': abort(400)
            try: subscription = parse_subscription(data.get('subscription'))
            except (ValueError, TypeError): abort(400)
            sid = digest(subscription['endpoint'])
            stale = db.scalars(select(PushSubscription.id).where(PushSubscription.role == role,
                PushSubscription.subject_id == uid, PushSubscription.binding == binding, PushSubscription.id != sid)).all()
            for old_id in stale: remove_subscription(db, old_id)
            row = db.get(PushSubscription, sid)
            if row and (row.role != role or row.subject_id != uid or row.binding != binding):
                # The same browser can change accounts. Never deliver an old account's queued alert.
                db.execute(delete(PushDelivery).where(PushDelivery.subscription_id == sid))
            count = db.scalar(select(func.count(PushSubscription.id)).where(PushSubscription.role == role,
                PushSubscription.subject_id == uid, PushSubscription.id != sid,
                PushSubscription.expires_at > datetime.utcnow()))
            if count >= 10: abort(429)
            if not row:
                row = PushSubscription(id=sid); db.add(row)
            row.role = role; row.subject_id = uid; row.binding = binding
            row.endpoint = subscription['endpoint']; row.p256dh = subscription['p256dh']; row.auth = subscription['auth']
            row.language = getattr(g, 'language', 'ru')
            row.doctor_grant = digest(g.doctor_grant) if role == 'doctor' else None
            row.expires_at = datetime.utcnow()+timedelta(days=30)
            db.commit()
        return jsonify(ok=True, enabled=True)


def enqueue_message(db, message, owner_id):
    from notification_patch import _admin_user_id
    role = 'owner' if message.role == 'doctor' else 'doctor'
    subject = owner_id if role == 'owner' else _admin_user_id()
    rows = db.scalars(select(PushSubscription).where(PushSubscription.role == role,
        PushSubscription.subject_id == subject, PushSubscription.expires_at > datetime.utcnow())).all()
    for row in rows:
        db.add(PushDelivery(subscription_id=row.id, binding=row.binding, message_id=message.id))


def claim_delivery():
    now = datetime.utcnow()
    with storage.SessionLocal() as db:
        row = db.scalar(select(PushDelivery).where(PushDelivery.state.in_(('pending', 'retry', 'sending')),
            PushDelivery.next_attempt <= now).order_by(PushDelivery.id).limit(1))
        if not row: return None
        changed = db.execute(update(PushDelivery).where(PushDelivery.id == row.id,
            PushDelivery.state.in_(('pending', 'retry', 'sending')), PushDelivery.next_attempt <= now).values(
            state='sending', attempts=PushDelivery.attempts+1, next_attempt=now+timedelta(minutes=2)),
            execution_options={'synchronize_session': False})
        if changed.rowcount != 1: db.rollback(); return None
        item_id = row.id; db.commit(); return item_id


class NoRedirectSession(Session):
    def request(self, method, url, **kwargs):
        if not valid_endpoint(url): raise ValueError('Unsupported push endpoint')
        kwargs['allow_redirects'] = False
        return super().request(method, url, **kwargs)


def deliver_one(item_id):
    from consultation_chat import ConsultationMessage, ConversationRead
    from doctor_access import DoctorAccess
    from notification_patch import _admin_user_id
    with storage.SessionLocal() as db:
        item = db.get(PushDelivery, item_id)
        if not item: return
        sub = db.get(PushSubscription, item.subscription_id)
        msg = db.get(ConsultationMessage, item.message_id)
        valid = sub and msg and sub.binding == item.binding and sub.expires_at > datetime.utcnow()
        if valid and sub.role == 'doctor':
            valid = sub.subject_id == _admin_user_id() and bool(db.scalar(select(DoctorAccess.digest).where(
                DoctorAccess.telegram_id == sub.subject_id, DoctorAccess.session_digest == sub.doctor_grant,
                DoctorAccess.used_at.is_not(None), DoctorAccess.session_expires_at > datetime.utcnow())))
        if valid:
            record = db.get(storage.Consultation, msg.consultation_id)
            valid = record and record.kind == 'consult_request' and msg.role != sub.role and (sub.role == 'doctor' or record.user_id == sub.subject_id)
            receipt = db.get(ConversationRead, msg.consultation_id)
            if receipt and getattr(receipt, sub.role+'_seen') >= msg.id: valid = False
        if not valid:
            item.state = 'skipped'; db.commit(); return
        payload = dict(language=sub.language, tag=f'mydoctor-{sub.role}-{msg.consultation_id}',
                       url=f"/{'doctor' if sub.role == 'doctor' else 'consultation'}/requests/{msg.consultation_id}#conversation")
        subscription = dict(endpoint=sub.endpoint, keys=dict(p256dh=sub.p256dh, auth=sub.auth))
        sid = sub.id; binding = sub.binding; attempts = item.attempts
    state = 'retry'; gone = False
    try:
        with NoRedirectSession() as transport:
            response = webpush(subscription_info=subscription, data=json.dumps(payload), vapid_private_key=vapid_keys()['private'],
                vapid_claims={'sub': os.getenv('MYDOCTOR_WEB_URL', 'https://mydoctor-web-production.up.railway.app')},
                ttl=3600, timeout=10, requests_session=transport)
            state = 'delivered' if 200 <= response.status_code < 300 else 'failed'
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else 0
        gone = status in (404, 410)
        if status in (400, 401, 403) or gone: state = 'failed'
        logging.getLogger(__name__).warning('Web push delivery %s: HTTP %s', item_id, status)
    except Exception as exc:
        logging.getLogger(__name__).warning('Web push delivery %s: %s', item_id, type(exc).__name__)
    with storage.SessionLocal() as db:
        item = db.get(PushDelivery, item_id)
        sub = db.get(PushSubscription, sid)
        if not item or not sub or sub.binding != binding: return
        if gone:
            remove_subscription(db, sid)
        else:
            item.state = 'failed' if state == 'retry' and attempts >= 8 else state
            item.next_attempt = datetime.utcnow()+timedelta(seconds=min(1800, 30*2**min(attempts, 6)))
        db.commit()


async def deliver_push_notifications():
    for _ in range(20):
        item_id = await asyncio.to_thread(claim_delivery)
        if item_id is None: break
        await asyncio.to_thread(deliver_one, item_id)
