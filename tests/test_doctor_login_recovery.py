from unittest.mock import patch
from sqlalchemy import select
import storage
with patch('openai.OpenAI'):
    import web_app
from doctor_access import create_doctor_link, DoctorAccess, DoctorWebAccount, hashed, telegram_login_url
from notification_patch import _admin_user_id
from test_web_app import reset_db, register
from test_doctor_portal import field
from test_doctor_entrypoints import bind, login


def test_owner_login_does_not_break_open_doctor_form_and_expired_session_recovers():
    reset_db(); c = web_app.app.test_client(); register(c)
    old = field(c.get('/doctor/login').get_data(as_text=True), 'doctor_csrf')
    assert c.post('/login', data={'email':'qa@example.com', 'password':'password123'}).status_code == 302
    response = c.post('/doctor/login', data={'doctor_csrf':old, 'email':'qa@example.com', 'password':'password123'})
    assert response.status_code == 403
    assert 'Email и пароль верны' in response.get_data(as_text=True)
    with c.session_transaction() as session:
        session.pop('doctor_csrf', None)
    response = c.post('/doctor/login', data={'doctor_csrf':old, 'email':'qa@example.com', 'password':'password123'})
    assert response.status_code == 400
    assert 'Форма входа обновлена' in response.get_data(as_text=True)
    fresh = field(response.get_data(as_text=True), 'doctor_csrf')
    response = c.post('/doctor/login', data={'doctor_csrf':fresh, 'email':'qa@example.com', 'password':'password123'})
    assert response.status_code == 403
    assert 'Email и пароль верны' in response.get_data(as_text=True)
    with storage.SessionLocal() as db:
        assert not db.scalar(select(DoctorWebAccount))


def test_first_telegram_login_guides_binding_then_separate_pwa_can_login():
    reset_db(); owner = web_app.app.test_client(); register(owner)
    telegram_browser = web_app.app.test_client()
    token = create_doctor_link(_admin_user_id())
    html = telegram_browser.get('/doctor/access/'+token).get_data(as_text=True)
    assert field(html, 'setup_email') == '1'
    response = telegram_browser.post('/doctor/access', data={'doctor_csrf':field(html, 'doctor_csrf'),
        'access_token':token, 'setup_email':'1', 'remember':'1'})
    assert response.status_code == 303 and response.headers['Location'] == '/doctor/account'
    assert bind(telegram_browser).status_code == 303
    pwa = web_app.app.test_client()
    assert login(pwa).status_code == 303
    # A new process/browser instance with only its persisted session cookie can reopen.
    reopened = web_app.app.test_client()
    reopened.set_cookie('session', pwa.get_cookie('session').value)
    assert reopened.get('/doctor/dashboard').status_code == 200
    assert reopened.get('/doctor/dashboard').status_code == 200


def test_stale_access_form_never_consumes_token_and_can_be_retried():
    reset_db(); c = web_app.app.test_client()
    token = create_doctor_link(_admin_user_id())
    response = c.post('/doctor/access', data={'access_token':token, 'doctor_csrf':'stale', 'setup_email':'1'})
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert 'Форма обновлена' in html
    with storage.SessionLocal() as db:
        assert db.get(DoctorAccess, hashed(token)).used_at is None
    assert c.post('/doctor/access', data={'access_token':token, 'doctor_csrf':field(html,'doctor_csrf'), 'setup_email':'1'}).headers['Location'] == '/doctor/account'


def test_only_known_legacy_origins_redirect_and_never_replay_posts(monkeypatch):
    reset_db(); monkeypatch.setenv('MYDOCTOR_CANONICAL_ORIGIN', 'https://mydoctor.vet')
    c = web_app.app.test_client()
    for host in ('mydoctor-web-production.up.railway.app','mydoctor-web-production-a7e2.up.railway.app'):
        response = c.get('/doctor/dashboard', base_url='https://'+host)
        assert response.status_code == 302
        assert response.headers['Location'] == 'https://mydoctor.vet/doctor/dashboard'
        assert response.headers['Referrer-Policy'] == 'no-referrer'
        response = c.post('/doctor/login', base_url='https://'+host, data={'email':'qa@example.com','password':'not-forwarded'})
        assert response.status_code == 303
        assert response.headers['Location'] == 'https://mydoctor.vet/doctor/login'
        assert c.get('/', base_url='https://'+host).status_code == 200
    assert c.get('/doctor/dashboard', base_url='https://mydoctor.vet').headers['Location'] == '/doctor/login'


def test_account_setup_telegram_target():
    reset_db(); storage.set_bot_setting('doctor_bot_username', 'my1doctor_bot')
    assert telegram_login_url('/doctor/account') == 'https://t.me/my1doctor_bot?start=doctor_account'
