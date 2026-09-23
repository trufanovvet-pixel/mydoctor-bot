import re
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
with patch('openai.OpenAI'):
    import web_app
from test_web_app import register, reset_db


@pytest.mark.parametrize('route', ['/login', '/register'])
def test_remembered_login_persists_across_browser_restarts_for_thirty_days(route):
    reset_db(); client = web_app.app.test_client()
    if route == '/login':
        register(client); client.get('/logout')
    response = client.post(route, data=dict(email='qa@example.com', password='password123', remember='1'))
    assert response.status_code == 302
    cookie_name = web_app.app.config['SESSION_COOKIE_NAME']
    cookie = client.get_cookie(cookie_name)
    assert timedelta(days=29) < cookie.expires-datetime.now(timezone.utc) <= timedelta(days=30)
    assert cookie.secure and cookie.http_only and cookie.same_site == 'Lax'
    with client.session_transaction() as session:
        assert session.permanent and session['uid']
    # A restarted browser retains only this persistent cookie, with no transient state.
    restarted = web_app.app.test_client(); restarted.set_cookie(cookie_name, cookie.value)
    assert restarted.get('/dashboard').status_code == 200
    assert restarted.get('/login?next=/messages').headers['Location'] == '/messages'
    assert restarted.get('/register').status_code == 302
    restarted.get('/logout')
    assert restarted.get_cookie(cookie_name) is None
    assert restarted.get('/dashboard').status_code == 302
    # The signed session is also rejected server-side when it ages past the limit.
    expired = web_app.app.test_client(); expired.set_cookie(cookie_name, cookie.value)
    with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=int(time.time())+31*86400):
        assert expired.get('/dashboard').status_code == 302


def test_unchecking_remember_reverts_to_a_browser_session_and_preserves_choice_on_error():
    reset_db(); client = web_app.app.test_client()
    client.post('/register', data=dict(email='qa@example.com', password='password123', remember='1'))
    response = client.post('/login', data=dict(email='qa@example.com', password='password123'))
    assert response.status_code == 302
    assert client.get_cookie(web_app.app.config['SESSION_COOKIE_NAME']).expires is None
    with client.session_transaction() as session: assert not session.permanent
    client.get('/logout')
    failed = client.post('/login', data=dict(email='qa@example.com', password='wrong-password')).get_data(as_text=True)
    checkbox = re.search(r'<input[^>]+name="remember"[^>]*>', failed).group()
    assert 'checked' not in checkbox
    assert client.get('/dashboard').status_code == 302


def test_remember_checkbox_is_visible_translated_and_checked_by_default():
    reset_db(); client = web_app.app.test_client()
    for route in ('/login', '/register'):
        html = client.get(route).get_data(as_text=True)
        checkbox = re.search(r'<input[^>]+name="remember"[^>]*>', html).group()
        assert 'type="checkbox"' in checkbox and 'checked' in checkbox
        assert 'Запомнить меня' in html and '30 дней' in html
    client.set_cookie('language', 'en')
    assert 'Stay signed in on this device for 30 days' in client.get('/login').get_data(as_text=True)
