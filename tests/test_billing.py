import io
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select
from pypdf import PdfWriter
import storage
import billing as b
from test_web_app import reset_db, register
from test_doctor_portal import doctor, field
import web_app


@pytest.fixture(autouse=True)
def clean():
    reset_db()


def owner(email='billing@example.com'):
    client = web_app.app.test_client()
    register(client, email)
    with client.session_transaction() as state:
        uid = state['uid']
    return client, uid


def enable():
    storage.set_bot_setting('billing_live', '1')
    with storage.SessionLocal() as db:
        method = b.PaymentMethod(name=b.CARD_TRANSFERS['mir']['name'], currency='RUB', instructions='TEST ONLY: receiving details')
        db.add(method)
        db.commit()
        return method.id


def order_for(client, method_id, plan='start'):
    html = client.get('/billing').get_data(as_text=True)
    data = {'billing_csrf': field(html, 'billing_csrf'), 'request_key': secrets.token_hex(16),
            'method_id': method_id, 'plan': plan, 'credits': '999999', 'amount_minor': '1'}
    result = client.post('/billing/orders', data=data)
    return result.headers['Location'].rsplit('/', 1)[1], data


def report(client, oid):
    html = client.get('/billing/orders/' + oid).get_data(as_text=True)
    return client.post('/billing/orders/' + oid, data={'billing_csrf': field(html, 'billing_csrf'),
                       'action': 'report', 'reference': 'TEST payment 12:00',
                       'receipt': (io.BytesIO(b'TEST RECEIPT'), 'receipt.jpg', 'image/jpeg')},
                       content_type='multipart/form-data')


def test_payment_report_requires_receipt_and_receipt_is_private_to_owner_or_doctor():
    c, uid = owner(); mid = enable(); oid, _ = order_for(c, mid)
    html = c.get('/billing/orders/' + oid).text
    csrf = field(html, 'billing_csrf')
    result = c.post('/billing/orders/' + oid, data={'billing_csrf': csrf, 'action': 'report',
                    'reference': 'TEST payment 12:00'})
    assert result.status_code == 303
    with storage.SessionLocal() as db:
        assert db.get(b.PaymentOrder, oid).status == 'awaiting'
        assert db.get(b.PaymentReceipt, oid) is None
    page = c.get('/billing/orders/' + oid).text
    assert 'Прикрепите чек' in page

    assert report(c, oid).status_code == 303
    with storage.SessionLocal() as db:
        receipt = db.get(b.PaymentReceipt, oid)
        assert receipt and receipt.filename == 'receipt.jpg' and receipt.data == b'TEST RECEIPT'
        assert db.get(b.PaymentOrder, oid).status == 'review'
    assert c.get('/billing/orders/' + oid + '/receipt').data == b'TEST RECEIPT'
    assert web_app.app.test_client().get('/billing/orders/' + oid + '/receipt').status_code == 403
    staff = doctor()
    assert staff.get('/billing/orders/' + oid + '/receipt').data == b'TEST RECEIPT'
    assert 'Открыть чек / скриншот' in staff.get('/doctor/payments/' + oid).text


def test_draft_does_not_open_payments_or_charge_existing_users():
    c, uid = owner()
    page = c.get('/billing')
    assert page.status_code == 200 and 'no-store' in page.headers['Cache-Control']
    assert 'Тестовый доступ без списаний' in page.text and 'Получить реквизиты' not in page.text
    with patch.object(web_app.client.responses, 'create', return_value=SimpleNamespace(output_text='Answer')):
        assert c.post('/api/chat', json={'message': 'General question'}).status_code == 200
    with storage.SessionLocal() as db:
        assert b.summary(db, uid)['balance'] == 0
        assert b.summary(db, uid)['requests'] == 1
        assert db.scalar(select(b.CreditUsage)).credits == 0


def test_manual_flow_trusts_server_price_and_requires_doctor_csrf_and_receipt_check():
    c, uid = owner(); mid = enable(); oid, data = order_for(c, mid)
    assert c.post('/billing/orders', data=data).headers['Location'].endswith(oid)
    assert c.post('/doctor/payments/' + oid, data={'action': 'confirm'}).status_code == 403
    assert report(c, oid).status_code == 303
    staff = doctor()
    html = staff.get('/doctor/payments/' + oid).text
    payload = {'billing_csrf': field(html, 'billing_csrf'), 'action': 'confirm'}
    assert staff.post('/doctor/payments/' + oid, data={**payload, 'billing_csrf': 'forged'}).status_code == 400
    staff.post('/doctor/payments/' + oid, data=payload)
    with storage.SessionLocal() as db:
        assert db.get(b.PaymentOrder, oid).status == 'review'
    for _ in range(2):
        assert staff.post('/doctor/payments/' + oid, data={**payload, 'received': '1'}).status_code == 303
    with storage.SessionLocal() as db:
        order = db.get(b.PaymentOrder, oid)
        assert order.amount_minor == 49900 and order.credits == 20
        assert order.checked_by and order.status == 'paid'
        grants = db.scalars(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:' + oid)).all()
        assert len(grants) == 1 and grants[0].remaining == 20
        assert timedelta(days=29, hours=23) < grants[0].expires_at - datetime.utcnow() <= timedelta(days=30)
    assert 'Баллы начислены.' in c.get('/billing/orders/' + oid).text


def test_owner_payment_isolation_and_cannot_mark_paid():
    c, uid = owner(); other, _ = owner('otherbilling@example.com'); mid = enable()
    oid, data = order_for(c, mid)
    assert other.get('/billing/orders/' + oid).status_code == 404
    html = other.get('/billing').text
    assert other.post('/billing/orders/' + oid, data={'billing_csrf': field(html, 'billing_csrf'), 'action': 'report'}).status_code == 404
    assert c.post('/billing/orders/' + oid, data={'billing_csrf': data['billing_csrf'], 'action': 'confirm'}).status_code == 409
    assert c.post('/billing/orders/' + oid, data={'action': 'report'}).status_code == 400
    assert web_app.app.test_client().get('/billing/orders/' + oid).status_code == 302
    assert oid not in other.get('/billing').text


def test_method_snapshot_stays_with_order_and_disabled_method_cannot_be_selected():
    c, uid = owner(); mid = enable(); oid, _ = order_for(c, mid)
    with storage.SessionLocal() as db:
        m = db.get(b.PaymentMethod, mid); m.instructions = 'New recipient'; m.enabled = False; db.commit()
    assert 'TEST ONLY: receiving details' in c.get('/billing/orders/' + oid).text
    with pytest.raises(b.BillingError):
        b.create_order(uid, 'start', mid, secrets.token_hex(16))


def test_trial_debits_replay_and_failed_calls_are_refunded():
    c, uid = owner(); enable(); c.get('/billing')
    result = SimpleNamespace(output_text='AI answer', model='gpt-5.6-sol', usage=SimpleNamespace(input_tokens=123, output_tokens=456))
    payload = {'message': 'Question', 'request_key': 'unique-request-0001'}
    with patch.object(web_app.client.responses, 'create', return_value=result) as ai:
        for _ in range(2):
            assert c.post('/api/chat', json=payload).json['answer'] == 'AI answer'
        assert ai.call_count == 1
    with storage.SessionLocal() as db:
        assert b.summary(db, uid)['balance'] == 4
        usage = db.scalar(select(b.CreditUsage))
        assert (usage.input_tokens, usage.output_tokens) == (123, 456)
    with patch.object(web_app.client.responses, 'create', side_effect=RuntimeError('unavailable')):
        assert c.post('/api/chat', json={'message': 'Another', 'request_key': 'unique-request-0002'}).status_code == 503
    with storage.SessionLocal() as db:
        assert b.summary(db, uid)['balance'] == 4
    assert c.post('/api/chat', json={**payload, 'message': 'Changed'}).status_code == 409


def test_concurrent_requests_never_overspend_and_refund_is_idempotent():
    c, uid = owner(); enable(); c.get('/billing')
    def take(i):
        try:
            usage, replay = b.reserve(uid, 'chat', f'concurrent-{i}', str(i).encode())
            return usage.id
        except b.BillingError as error:
            assert error.status == 402
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(take, range(6)))
    assert len([x for x in ids if x]) == 5
    with storage.SessionLocal() as db:
        assert b.summary(db, uid)['balance'] == 0
        assert len(db.scalars(select(b.CreditUsage)).all()) == 5
    for usage_id in filter(None, ids):
        b.fail(uid, usage_id); b.fail(uid, usage_id)
    with storage.SessionLocal() as db:
        assert b.summary(db, uid)['balance'] == 5


def test_concurrent_payment_confirmations_only_grant_once():
    c, uid = owner(); mid = enable(); oid, _ = order_for(c, mid); report(c, oid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: b.confirm_order(uid, oid, 123), range(4)))
    with storage.SessionLocal() as db:
        assert len(db.scalars(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:' + oid)).all()) == 1


def test_expiry_and_paused_sales_do_not_restore_free_access_to_paid_users():
    c, uid = owner(); mid = enable(); oid, _ = order_for(c, mid); report(c, oid); b.confirm_order(uid, oid, 123)
    with storage.SessionLocal() as db:
        for grant in db.scalars(select(b.CreditGrant)).all():
            grant.remaining = 0
            if grant.origin.startswith('payment:'):
                grant.remaining = 20; grant.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    storage.set_bot_setting('billing_live', '0')
    with patch.object(web_app.client.responses, 'create') as ai:
        assert c.post('/api/chat', json={'message': 'Question'}).status_code == 402
        ai.assert_not_called()
    assert 'Срок истёк' in c.get('/billing').text


def test_crash_reservation_is_released_on_next_billing_visit():
    c, uid = owner(); enable(); c.get('/billing')
    usage, _ = b.reserve(uid, 'document', 'crash-reserve-0001', b'data')
    with storage.SessionLocal() as db:
        db.get(b.CreditUsage, usage.id).created_at = datetime.utcnow() - timedelta(minutes=11)
        db.commit()
    c.get('/billing')
    with storage.SessionLocal() as db:
        assert b.summary(db, uid)['balance'] == 5
        assert db.get(b.CreditUsage, usage.id).status == 'failed'
    usage2, _ = b.reserve(uid, 'document', 'crash-reserve-0001', b'data')
    assert usage2.id == usage.id
    b.fail(uid, usage2.id)


def test_paid_pdf_page_limit_rejects_before_charging_and_document_errors_refund():
    c, uid = owner(); enable(); c.get('/billing')
    writer = PdfWriter()
    for _ in range(6): writer.add_blank_page(width=100, height=100)
    output = io.BytesIO(); writer.write(output)
    with patch.object(web_app.client.responses, 'create') as ai:
        result = c.post('/documents', data={'file': (io.BytesIO(output.getvalue()), 'six-pages.pdf')}, content_type='multipart/form-data')
        assert result.status_code == 302
        ai.assert_not_called()
    with patch.object(web_app.client.responses, 'create', side_effect=RuntimeError('unavailable')):
        assert c.post('/documents', data={'file': (io.BytesIO(b'image'), 'photo.png')}, content_type='multipart/form-data').status_code == 302
    with storage.SessionLocal() as db:
        assert b.summary(db, uid)['balance'] == 5
        assert db.scalar(select(web_app.WebDocument)).analysis


def test_card_configuration_is_doctor_only_and_cannot_enable_without_receiving_details():
    c, _ = owner(); staff = doctor()
    assert c.post('/doctor/payments/methods', data={}).status_code == 403
    html = staff.get('/doctor/payments').text
    data = {'billing_csrf': field(html, 'billing_csrf'), 'transfer': 'mir', 'currency': 'USDT', 'instructions': 'TEST ONLY: receiving details'}
    staff.post('/doctor/payments/methods', data=data)
    with storage.SessionLocal() as db: assert not db.scalar(select(b.PaymentMethod.id))
    staff.post('/doctor/payments', data={'billing_csrf': data['billing_csrf'], 'enabled': '1'})
    assert not b.live()
    staff.post('/doctor/payments/methods', data={**data, 'currency': 'RUB', 'billing_csrf': 'forged'})
    with storage.SessionLocal() as db: assert not db.scalar(select(b.PaymentMethod.id))
    staff.post('/doctor/payments/methods', data={**data, 'currency': 'RUB'})
    with storage.SessionLocal() as db: assert db.scalar(select(b.PaymentMethod)).name == b.CARD_TRANSFERS['mir']['name']
    assert not b.live()
    staff.post('/doctor/payments', data={'billing_csrf': data['billing_csrf'], 'enabled': '1'})
    assert b.live()


def test_mastercard_prices_are_exact_and_old_orders_keep_currency_recipient_and_price():
    c, uid = owner(); enable()
    mid = b.save_card_method('mastercard', 'BYN', 'TEST ONLY: first recipient', {'start': '19,90', 'care': '40', 'family': '80.01'})
    html = c.get('/billing?transfer=mastercard').text
    assert '19.90 BYN' in html and '80.01 BYN' in html
    oid, _ = order_for(c, mid)
    replacement = b.save_card_method('mastercard', 'KZT', 'TEST ONLY: second recipient', {'start': '3000', 'care': '6000', 'family': '12000'})
    with storage.SessionLocal() as db:
        order = db.get(b.PaymentOrder, oid)
        assert (order.amount_minor, order.currency, order.instructions) == (1990, 'BYN', 'TEST ONLY: first recipient')
        assert not db.get(b.PaymentMethod, mid).enabled
        assert b.card_methods(db)['mastercard']['method'].id == replacement
    with pytest.raises(b.BillingError): b.create_order(uid, 'start', mid, secrets.token_hex(16))
    assert report(c, oid).status_code == 303
    b.confirm_order(uid, oid, 123)
    with storage.SessionLocal() as db:
        assert db.scalar(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:' + oid)).total == 20


@pytest.mark.parametrize('value', ['', '0', '-1', 'nan', 'inf', '1e2', '1.001', '1000000.01'])
def test_invalid_mastercard_price_does_not_replace_active_method(value):
    original = b.save_card_method('mastercard', 'BYN', 'TEST ONLY: original recipient', {'start': '20', 'care': '40', 'family': '80'})
    with pytest.raises(b.BillingError):
        b.save_card_method('mastercard', 'BYN', 'TEST ONLY: replacement recipient', {'start': value, 'care': '40', 'family': '80'})
    with storage.SessionLocal() as db:
        assert b.card_methods(db)['mastercard']['method'].id == original


def test_incomplete_and_legacy_methods_cannot_create_new_orders():
    c, uid = owner(); enable()
    with storage.SessionLocal() as db:
        incomplete = b.PaymentMethod(name=b.CARD_TRANSFERS['mastercard']['name'], currency='BYN', instructions='TEST ONLY: recipient')
        legacy = b.PaymentMethod(name='USDT test', currency='USDT', instructions='TEST ONLY: wallet', network='TEST')
        db.add_all([incomplete, legacy]); db.commit()
        ids = [incomplete.id, legacy.id]
    for mid in ids:
        with pytest.raises(b.BillingError): b.create_order(uid, 'start', mid, secrets.token_hex(16))
    html = c.get('/billing?transfer=mastercard').text
    assert 'name="method_id"' not in html and 'USDT' not in html
    assert 'Российская карта' in html and 'Перевод на Mastercard' in html


def test_only_latest_transfer_details_can_be_reenabled_and_are_translated():
    staff = doctor()
    old = b.save_card_method('mir', 'RUB', 'TEST ONLY: first recipient', {})
    latest = b.save_card_method('mir', 'RUB', 'TEST ONLY: new recipient', {})
    csrf = field(staff.get('/doctor/payments').text, 'billing_csrf')
    assert staff.post(f'/doctor/payments/methods/{old}/toggle', data={'billing_csrf': csrf, 'enabled': '1'}).status_code == 409
    assert staff.post(f'/doctor/payments/methods/{latest}/toggle', data={'billing_csrf': csrf, 'enabled': '0'}).status_code == 303
    c, _ = owner(); c.set_cookie('language', 'en')
    html = c.get('/billing').text
    assert 'Russian card' in html and 'Transfer to a Mastercard' in html
    assert 'Перевод на' not in html


def test_document_success_costs_five_and_retry_does_not_repeat_analysis():
    c, uid = owner(); enable(); c.get('/billing')
    def send():
        return c.post('/documents', data={'request_key': 'document-success-001',
                      'file': (io.BytesIO(b'image'), 'photo.png')}, content_type='multipart/form-data')
    with patch.object(web_app.client.responses, 'create', return_value=SimpleNamespace(output_text='Results')) as ai:
        assert send().status_code == 302
        assert send().status_code == 302
        assert ai.call_count == 1
    with storage.SessionLocal() as db:
        assert b.summary(db, uid)['balance'] == 0
        assert len(db.scalars(select(web_app.WebDocument)).all()) == 1
        assert db.scalar(select(b.CreditUsage)).status == 'success'


def test_document_save_failure_rolls_back_document_and_refunds_immediately():
    c, uid = owner(); enable(); c.get('/billing')
    with patch.object(web_app.client.responses, 'create', return_value=SimpleNamespace(output_text='Results')), \
         patch.object(b, 'complete', side_effect=RuntimeError('storage unavailable')):
        result = c.post('/documents', data={'file': (io.BytesIO(b'image'), 'photo.png')}, content_type='multipart/form-data')
        assert result.status_code == 302
    with storage.SessionLocal() as db:
        assert b.summary(db, uid)['balance'] == 5
        assert not db.scalar(select(web_app.WebDocument))
        assert db.scalar(select(b.CreditUsage)).status == 'failed'


def test_english_billing_and_doctor_return_paths():
    c, _ = owner(); c.set_cookie('language', 'en')
    html = c.get('/billing').text
    assert 'Plan &amp; payments' in html and 'Payments are not open yet.' in html
    assert 'Тариф и оплаты' not in html.split('<body', 1)[-1]
    from doctor_access import doctor_destination
    assert doctor_destination('/doctor/payments') == '/doctor/payments'
    assert doctor_destination('/doctor/payments/' + 'a'*32).startswith('/doctor/payments/')
    assert doctor_destination('/doctor/payments/methods') == '/doctor'


def test_doctor_telegram_login_returns_to_the_requested_payment():
    import asyncio
    import doctor_access
    from unittest.mock import AsyncMock
    storage.set_bot_setting('doctor_bot_username', 'example_bot')
    oid = 'a' * 32
    for target, payload in [('/doctor/payments', 'doctor_payments'),
                            ('/doctor/payments/' + oid, 'doctor_payment_' + oid)]:
        assert doctor_access.telegram_login_url(target).endswith('start=' + payload)
        with patch.object(doctor_access, 'doctor_command', new_callable=AsyncMock) as command:
            update = SimpleNamespace()
            context = SimpleNamespace(args=[payload])
            asyncio.run(doctor_access.doctor_start_handler(AsyncMock())(update, context))
            command.assert_awaited_once_with(update, context, target)


def allowance_at(uid, now):
    with patch.object(b, 'datetime', wraps=datetime) as clock:
        clock.utcnow.return_value = now
        with storage.SessionLocal() as db:
            b.lock_user(db, uid)
            b.trial(db, uid)
            db.commit()
            return b.summary(db, uid)['balance']


def test_monthly_free_credits_rollover_no_backfill_and_keep_paid_balance():
    c, uid = owner(); enable()
    with storage.SessionLocal() as db:
        db.add(b.CreditGrant(user_id=uid, origin='payment:test', total=20, remaining=17,
                            expires_at=datetime(2027, 5, 1)))
        db.commit()
    assert allowance_at(uid, datetime(2026, 12, 31, 23, 59)) == 22
    with storage.SessionLocal() as db:
        free = db.scalar(select(b.CreditGrant).where(b.CreditGrant.origin.like('monthly:%')))
        free.remaining = 2
        db.commit()
    assert allowance_at(uid, datetime(2026, 12, 31, 23, 59)) == 19
    assert allowance_at(uid, datetime(2027, 1, 1)) == 22
    assert allowance_at(uid, datetime(2027, 1, 1)) == 22
    assert allowance_at(uid, datetime(2027, 4, 1)) == 22
    with storage.SessionLocal() as db:
        grants = db.scalars(select(b.CreditGrant).where(b.CreditGrant.origin.like('monthly:%'))).all()
        assert len(grants) == 3  # No credit accumulation for February and March.
        assert db.scalar(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:test')).remaining == 17


def test_existing_trial_is_not_granted_twice_during_migration():
    c, uid = owner()
    with storage.SessionLocal() as db:
        db.add(b.CreditGrant(user_id=uid, origin=f'trial:{uid}', total=5, remaining=1,
                            created_at=datetime(2026, 9, 10)))
        db.commit()
    assert allowance_at(uid, datetime(2026, 9, 24)) == 1
    assert allowance_at(uid, datetime(2026, 10, 1)) == 5


def test_old_trial_expires_and_concurrent_monthly_grants_are_unique():
    c, uid = owner()
    with storage.SessionLocal() as db:
        db.add(b.CreditGrant(user_id=uid, origin=f'trial:{uid}', total=5, remaining=3,
                            created_at=datetime(2020, 1, 1)))
        db.commit()
    now = datetime.utcnow()
    def grant(_):
        with storage.SessionLocal() as db:
            b.lock_user(db, uid); b.trial(db, uid); db.commit()
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(grant, range(5)))
    with storage.SessionLocal() as db:
        assert b.summary(db, uid)['balance'] == 5
        assert len(db.scalars(select(b.CreditGrant).where(b.CreditGrant.origin.like('monthly:%'))).all()) == 1
        assert db.scalar(select(b.CreditGrant).where(b.CreditGrant.origin == f'trial:{uid}')).expires_at <= now


def test_pet_and_prevention_remain_free_with_zero_credits():
    from prevention_patch import PreventiveEvent
    c, uid = owner(); enable(); c.get('/billing')
    with storage.SessionLocal() as db:
        for grant in db.scalars(select(b.CreditGrant).where(b.CreditGrant.user_id == uid)):
            grant.remaining = 0
        db.commit()
    assert c.post('/pets', data={'name': 'Free pet', 'species': 'Собака', 'weight': '10'}).status_code == 302
    with storage.SessionLocal() as db:
        pid = db.scalar(select(storage.Pet.id).where(storage.Pet.user_id == uid))
    assert c.get('/pets').status_code == 200
    assert c.get(f'/pets/{pid}').status_code == 200
    assert c.get('/prevention').status_code == 200
    assert c.post('/prevention', data={'pet_id': pid, 'kind': 'vaccination', 'due_date': '2027-01-01'}).status_code == 302
    with storage.SessionLocal() as db:
        assert db.scalar(select(PreventiveEvent).where(PreventiveEvent.user_id == uid))
        assert b.summary(db, uid)['balance'] == 0
        assert not db.scalar(select(b.CreditUsage.id))
    html = c.get('/billing').text
    assert 'Каждый месяц — 5 бесплатных баллов' in html
    assert 'всегда бесплатны' in html
