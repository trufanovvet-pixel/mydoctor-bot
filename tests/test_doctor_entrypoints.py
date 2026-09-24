from sqlalchemy import select
import storage
from unittest.mock import patch
with patch('openai.OpenAI'):
    import web_app
from doctor_access import DoctorWebAccount
from test_doctor_portal import doctor, field, request_with_pet_and_documents
from test_web_app import reset_db, register


def bind(staff, email='qa@example.com'):
    page = staff.get('/doctor/account').get_data(as_text=True)
    return staff.post('/doctor/account', data=dict(doctor_csrf=field(page, 'doctor_csrf'),
        email=email, password='password123'))


def login(staff, email='qa@example.com', **extra):
    page = staff.get('/doctor/login').get_data(as_text=True)
    return staff.post('/doctor/login', data=dict(doctor_csrf=field(page, 'doctor_csrf'),
        email=email, password='password123', remember='1', **extra))


def test_distinct_apps_and_doctor_login_stays_in_doctor_shell():
    reset_db(); browser = web_app.app.test_client()
    assert browser.get('/').status_code == 200
    for path in ('/doctor', '/doctor/', '/doctor/dashboard'):
        response = browser.get(path, follow_redirects=True)
        assert response.request.path == '/doctor/login'
        assert b'/doctor.webmanifest' in response.data
        assert b'action="/doctor/login"' in response.data
    manifest = browser.get('/doctor.webmanifest')
    assert manifest.json['id'] == '/doctor'
    assert manifest.json['start_url'].startswith(manifest.json['scope'])
    assert manifest.headers['Cache-Control'] == 'no-cache'
    assert browser.get('/manifest.webmanifest').json['start_url'] == '/dashboard'
    page = browser.get('/doctor/install').get_data(as_text=True)
    assert 'apple-mobile-web-app-title" content="Мой доктор Врач"' in page
    assert 'data-push-button' not in page
    assert browser.get('/app-icon/doctor/180.png').data != browser.get('/app-icon/180.png').data


def test_client_cannot_self_promote_or_use_doctor_api():
    reset_db(); client = web_app.app.test_client(); register(client)
    assert client.post('/doctor/account', data={'role': 'doctor'}).status_code == 403
    assert login(client).status_code == 403
    assert client.get('/doctor/dashboard').status_code == 302
    assert client.get('/doctor/requests/1/messages').status_code == 403
    assert client.get('/api/push/config?role=doctor').status_code == 403
    with storage.SessionLocal() as db:
        assert not db.scalar(select(DoctorWebAccount))


def test_verified_binding_password_login_real_records_and_reload():
    reset_db(); owner, uid, rid, data = request_with_pet_and_documents()
    with storage.SessionLocal() as db:
        email = db.scalar(select(web_app.WebAccount.email).where(web_app.WebAccount.user_id == uid))
    staff = doctor()
    assert bind(staff, email).status_code == 303
    fresh = web_app.app.test_client()
    assert login(fresh, email).status_code == 303
    for _ in range(2):
        page = fresh.get('/doctor/dashboard')
        assert page.status_code == 200
        assert f'/doctor/requests/{rid}'.encode() in page.data
    assert fresh.get('/').headers['Location'] == '/doctor'
    # The owner PWA's explicit entry point remains the owner app, even for a doctor.
    assert b'/manifest.webmanifest' in fresh.get('/dashboard').data
    case = fresh.get(f'/doctor/requests/{rid}')
    assert b'Archie' in case.data and b'cbc.pdf' in case.data
    assert fresh.get(f'/doctor/requests/{rid}/messages').status_code == 200
    assert fresh.get('/api/push/config?role=doctor').status_code == 200
    page = fresh.get('/doctor').get_data(as_text=True)
    assert fresh.post('/doctor/logout', data={'doctor_csrf': field(page, 'doctor_csrf')}).status_code == 303
    assert fresh.get('/doctor').status_code == 302
    response = fresh.post('/login', data={'email': email, 'password': 'password123', 'remember': '1'})
    assert response.headers['Location'] == '/doctor'


def test_binding_requires_csrf_and_existing_account_password():
    reset_db(); owner = web_app.app.test_client(); register(owner)
    staff = doctor()
    assert staff.post('/doctor/account', data={'email': 'qa@example.com', 'password': 'password123'}).status_code == 400
    page = staff.get('/doctor/account').get_data(as_text=True)
    assert staff.post('/doctor/account', data={'doctor_csrf': field(page, 'doctor_csrf'),
        'email': 'qa@example.com', 'password': 'wrong'}).status_code == 400
    with storage.SessionLocal() as db:
        assert not db.scalar(select(DoctorWebAccount))


def test_new_client_login_revokes_previous_doctor_access():
    reset_db(); owner = web_app.app.test_client(); register(owner)
    staff = doctor()
    assert staff.post('/login', data={'email': 'qa@example.com', 'password': 'password123'}).status_code == 302
    assert staff.get('/doctor/dashboard').status_code == 302
    assert staff.get('/doctor/requests/1/messages').status_code == 403


def test_doctor_password_attempts_limited_across_browser_sessions():
    reset_db()
    for _ in range(10):
        assert login(web_app.app.test_client(), 'unknown@example.com').status_code == 403
    assert login(web_app.app.test_client(), 'unknown@example.com').status_code == 429
    assert web_app.app.test_client().post('/login', data={'email': 'unknown@example.com', 'password': 'bad'}).status_code == 429
