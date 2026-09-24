import io
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from PIL import Image
from pywebpush import WebPushException
from sqlalchemy import func, select

import storage
with patch('openai.OpenAI'):
    import web_app
import web_push as push
from consultation_chat import ConversationRead
from doctor_access import DoctorAccess
from test_consultation_chat import setup, send
from test_doctor_portal import doctor, field
from test_web_app import register, reset_db


def subscription(endpoint=None):
    key = ec.generate_private_key(ec.SECP256R1())
    return dict(endpoint=endpoint or 'https://fcm.googleapis.com/fcm/send/'+secrets.token_hex(16),
                keys=dict(p256dh=push.b64(key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)),
                          auth=push.b64(secrets.token_bytes(16))))


def enable(client, role='owner', value=None):
    value = value or subscription()
    config = client.get('/api/push/config?role='+role)
    assert config.status_code == 200
    response = client.post('/api/push/subscription', json=dict(role=role, action='enable', subscription=value),
                           headers={'X-PWA-CSRF': config.json['csrf']})
    assert response.status_code == 200
    return value, config.json


def test_installation_entrypoints_icons_and_private_cache_policy():
    reset_db(); anon = web_app.app.test_client()
    assert anon.get('/install').status_code == 200
    assert anon.get('/doctor/install').status_code == 200
    owner_manifest = anon.get('/manifest.webmanifest').json
    doctor_manifest = anon.get('/doctor.webmanifest').json
    assert owner_manifest['display'] == 'standalone'
    assert owner_manifest['start_url'] == '/dashboard' and doctor_manifest['start_url'] == '/doctor/dashboard'
    assert doctor_manifest['scope'] == '/doctor'
    assert owner_manifest['id'] != doctor_manifest['id']
    assert anon.get('/manifest.webmanifest?lang=en').json['name'] == 'MyDoctor'
    for icon in owner_manifest['icons']:
        response = anon.get(icon['src'])
        image = Image.open(io.BytesIO(response.data))
        assert f'{image.width}x{image.height}' == icon['sizes'] and image.format == 'PNG'
    assert anon.get('/app-icon/181.png').status_code == 404
    assert anon.get('/service-worker.js').headers['Cache-Control'] == 'no-cache'
    assert anon.get('/service-worker.js').headers['Service-Worker-Allowed'] == '/'
    assert anon.get('/static/offline.html').status_code == 200
    owner, uid, rid, staff = setup()
    for url in ('/dashboard', '/pets', '/install', f'/consultation/requests/{rid}'):
        response = owner.get(url)
        assert 'no-store' in response.headers['Cache-Control']
        assert 'rel="manifest"' in response.get_data(as_text=True)
    page = staff.get('/doctor/install').get_data(as_text=True)
    assert '/doctor.webmanifest?lang=ru' in page and 'data-role="doctor"' in page
    assert 'data-role="doctor"' not in owner.get('/install').get_data(as_text=True)
    owner.set_cookie('language', 'en')
    page = owner.get('/install').get_data(as_text=True)
    assert 'Enable notifications' in page and 'Add to your home screen' in page
    assert 'Включить уведомления' not in page.split('<body', 1)[1]


def test_push_authentication_csrf_and_endpoint_validation():
    reset_db(); client = web_app.app.test_client()
    assert client.get('/api/push/config').status_code == 401
    register(client)
    assert client.get('/api/push/config?role=doctor').status_code == 403
    config = client.get('/api/push/config').json
    assert 'private' not in config and config['enabled'] is False
    assert client.post('/api/push/subscription', json=dict(action='enable', subscription=subscription())).status_code == 400
    headers = {'X-PWA-CSRF': config['csrf']}
    for endpoint in ('http://fcm.googleapis.com/fcm/send/x', 'https://localhost/push', 'https://127.0.0.1/push',
                     'https://fcm.googleapis.com.attacker.test/push', 'https://fcm.googleapis.com@localhost/x',
                     'https://web.push.apple.com:8080/x', 'https://fcm.googleapis.com/x?redirect=localhost'):
        assert client.post('/api/push/subscription', json=dict(action='enable', subscription=subscription(endpoint)), headers=headers).status_code == 400
    bad = subscription(); bad['keys']['p256dh'] = push.b64(b'\x04'+b'\x00'*64)
    assert client.post('/api/push/subscription', json=dict(action='enable', subscription=bad), headers=headers).status_code == 400
    assert client.post('/api/push/subscription', json=dict(role='doctor', action='enable', subscription=subscription()), headers=headers).status_code == 403
    assert client.get('/api/push/config?role=attacker').status_code == 400
    value, config = enable(client)
    assert config['public_key'] == client.get('/api/push/config').json['public_key']
    assert client.get('/api/push/config').json['enabled'] is True
    with storage.SessionLocal() as db:
        assert db.scalar(select(func.count(push.PushSubscription.id))) == 1
        assert push.vapid_keys()['public'] == config['public_key']


@pytest.mark.asyncio
async def test_bidirectional_push_only_to_recipient_with_generic_encrypted_payload():
    owner, uid, rid, staff = setup()
    owner_sub, _ = enable(owner)
    doctor_sub, _ = enable(staff, 'doctor')
    other = web_app.app.test_client(); register(other, 'other@example.com'); enable(other)
    first = send(owner, rid, 'Secret medical question').json['message']['id']
    second = send(staff, rid, 'Private medical answer', role='doctor').json['message']['id']
    with storage.SessionLocal() as db:
        jobs = db.scalars(select(push.PushDelivery).order_by(push.PushDelivery.id)).all()
        assert [(job.message_id, job.subscription_id) for job in jobs] == [
            (first, push.digest(doctor_sub['endpoint'])), (second, push.digest(owner_sub['endpoint']))]
    # Keep actual encryption and VAPID signing. Only the HTTP boundary is replaced.
    with patch.object(push.NoRedirectSession, 'request', return_value=SimpleNamespace(status_code=201, text='', headers={})) as transport:
        await push.deliver_push_notifications()
        assert transport.call_count == 2
        for call in transport.call_args_list:
            assert call.kwargs['timeout'] == 10
            assert call.kwargs['headers']['content-encoding'] == 'aes128gcm'
            assert call.kwargs['headers']['Authorization'].startswith('vapid ')
            assert isinstance(call.kwargs['data'], bytes)
            assert b'Secret medical' not in call.kwargs['data']
    with storage.SessionLocal() as db:
        assert set(db.scalars(select(push.PushDelivery.state))) == {'delivered'}
    with patch('web_push.webpush') as webpush:
        await push.deliver_push_notifications(); webpush.assert_not_called()


def test_disable_logout_and_account_change_revoke_browser_only():
    owner, uid, rid, staff = setup()
    value, config = enable(owner); enable(staff, 'doctor')
    other = web_app.app.test_client(); register(other, 'another@example.com'); enable(other)
    send(staff, rid, 'Notification pending', role='doctor')
    disabled = owner.post('/api/push/subscription', json=dict(action='disable'), headers={'X-PWA-CSRF': config['csrf']})
    assert disabled.json['enabled'] is False
    with storage.SessionLocal() as db:
        assert not db.get(push.PushSubscription, push.digest(value['endpoint']))
        assert not db.scalar(select(push.PushDelivery))
        assert db.scalar(select(func.count(push.PushSubscription.id))) == 2
    enable(owner); owner.get('/logout')
    with storage.SessionLocal() as db:
        assert not db.scalar(select(push.PushSubscription).where(push.PushSubscription.subject_id == uid))
    page = staff.get('/doctor').get_data(as_text=True)
    assert staff.post('/doctor/logout', data={'doctor_csrf': field(page, 'doctor_csrf')}).status_code == 303
    with storage.SessionLocal() as db:
        assert db.scalar(select(func.count(push.PushSubscription.id))) == 1
    # Signing into another owner account must also revoke old browser bindings.
    assert other.post('/login', data=dict(email='booking@example.com', password='password123')).status_code == 302
    with storage.SessionLocal() as db: assert not db.scalar(select(push.PushSubscription))


@pytest.mark.asyncio
async def test_read_messages_expired_grants_and_rebound_endpoints_are_not_delivered():
    owner, uid, rid, staff = setup(); enable(owner); enable(staff, 'doctor')
    mid = send(staff, rid, 'Already read', role='doctor').json['message']['id']
    with storage.SessionLocal() as db:
        db.add(ConversationRead(consultation_id=rid, owner_seen=mid, doctor_seen=0)); db.commit()
    send(owner, rid, 'Doctor signed out')
    with storage.SessionLocal() as db:
        for grant in db.scalars(select(DoctorAccess)): grant.session_expires_at = datetime.utcnow()-timedelta(seconds=1)
        db.commit()
    with patch('web_push.webpush') as webpush:
        await push.deliver_push_notifications(); webpush.assert_not_called()
    with storage.SessionLocal() as db: assert set(db.scalars(select(push.PushDelivery.state))) == {'skipped'}
    # Even knowing a shared endpoint cannot carry an old account's pending alert forward.
    staff = doctor(); shared, _ = enable(owner)
    send(staff, rid, 'Old owner', role='doctor')
    other = web_app.app.test_client(); register(other, 'new-browser-user@example.com'); enable(other, value=shared)
    with patch('web_push.webpush') as webpush:
        await push.deliver_push_notifications(); webpush.assert_not_called()


@pytest.mark.asyncio
async def test_push_retry_claim_and_expired_subscription_cleanup():
    owner, uid, rid, staff = setup(); value, _ = enable(owner)
    send(staff, rid, 'Retry delivery', role='doctor')
    job = push.claim_delivery(); assert job and push.claim_delivery() is None
    with patch('web_push.webpush', side_effect=TimeoutError): push.deliver_one(job)
    with storage.SessionLocal() as db:
        row = db.get(push.PushDelivery, job); assert row.state == 'retry' and row.attempts == 1
        row.next_attempt = datetime.utcnow()-timedelta(seconds=1); db.commit()
    with patch('web_push.webpush', side_effect=WebPushException('gone', response=SimpleNamespace(status_code=410))):
        await push.deliver_push_notifications()
    with storage.SessionLocal() as db:
        assert not db.get(push.PushSubscription, push.digest(value['endpoint']))
        assert not db.get(push.PushDelivery, job)


def test_push_transport_never_follows_redirects_or_posts_to_untrusted_hosts():
    transport = push.NoRedirectSession()
    with patch('requests.Session.request') as request:
        transport.post('https://web.push.apple.com/example', data=b'encrypted', timeout=10)
        assert request.call_args.kwargs['allow_redirects'] is False
        with pytest.raises(ValueError): transport.post('http://127.0.0.1/admin')
        assert request.call_count == 1
