import io
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pypdf import PdfWriter
from PIL import Image
from sqlalchemy import select

import billing as b
import storage
import telegram_billing as tb
import web_auth
with patch('openai.OpenAI'):
    import web_app
from test_billing import owner, order_for, report
from test_doctor_portal import doctor, field
from test_web_app import reset_db


@pytest.fixture(autouse=True)
def clean():
    reset_db()


def setup_sbp():
    mid = b.save_card_method('sbp', 'RUB', 'TEST ONLY СБП: +7 000 000 00 00, тестовый банк, Получатель', {})
    storage.set_bot_setting('billing_live', '1')
    return mid


def sign_in(client, token, transfer='sbp'):
    path = f'/login/{token}?next=/billing&transfer={transfer}'
    page = client.get(path)
    assert page.status_code == 200
    return client.post(path, data={'login_csrf': field(page.text, 'login_csrf'), 'remember': '1'})


def test_sbp_checkout_and_legacy_russian_card_replacement():
    c, uid = owner()
    mid = setup_sbp()
    html = c.get('/billing?transfer=sbp').text
    assert 'Российская карта' in html and 'СБП' in html and 'Mastercard' in html
    assert '499 ₽' in html
    oid, _ = order_for(c, mid)
    assert 'TEST ONLY СБП' in c.get('/billing/orders/' + oid).text
    report(c, oid)
    b.confirm_order(uid, oid, 123)
    b.confirm_order(uid, oid, 123)
    with storage.SessionLocal() as db:
        assert db.get(b.PaymentOrder, oid).method_name == 'СБП'
        assert b.summary(db, uid)['balance'] == 25
        old = b.PaymentMethod(name='Перевод на карту Мир', currency='RUB', instructions='TEST ONLY old card')
        db.add(old); db.commit(); old_id = old.id
        assert b.card_methods(db)['mir']['method'].id == old_id
    latest = b.save_card_method('mir', 'RUB', 'TEST ONLY new Russian card', {})
    with storage.SessionLocal() as db:
        assert not db.get(b.PaymentMethod, old_id).enabled
        assert b.card_methods(db)['mir']['method'].id == latest
    staff = doctor(); csrf = field(staff.get('/doctor/payments').text, 'billing_csrf')
    assert staff.post(f'/doctor/payments/methods/{old_id}/toggle', data={'billing_csrf': csrf, 'enabled': '1'}).status_code == 409
    with pytest.raises(b.BillingError):
        b.save_card_method('sbp', 'USD', 'TEST ONLY receiving details', {})


def test_telegram_checkout_link_survives_preview_requires_csrf_and_only_signs_into_its_user():
    storage.ensure_user(701, 'test_user', 'Telegram owner')
    uid = tb.account_id(701)
    token = web_auth.create_token(701)
    c, other_uid = owner()
    path = f'/login/{token}?next=/billing&transfer=sbp'
    for _ in range(2):
        page = c.get(path)
        assert page.status_code == 200 and 'Сейчас открыт другой аккаунт' in page.text
        assert page.headers['Referrer-Policy'] == 'no-referrer'
        with c.session_transaction() as state: assert state['uid'] == other_uid
    assert c.post(path, data={'login_csrf': 'wrong'}).status_code == 400
    assert web_auth.peek_token(token) == 701
    result = c.post(path, data={'login_csrf': field(page.text, 'login_csrf'), 'uid': other_uid, 'remember': '1'})
    assert result.status_code == 303 and result.location == '/billing?transfer=sbp'
    with c.session_transaction() as state: assert state['uid'] == uid and state.permanent
    assert web_app.app.test_client().get(path).status_code == 410
    fresh = web_auth.create_token(701)
    assert c.get(f'/login/{fresh}?next=/billing&transfer=sbp').location == '/billing?transfer=sbp'
    with storage.SessionLocal() as db:
        row = db.scalar(select(web_auth.WebLoginToken).where(web_auth.WebLoginToken.token == web_auth.digest(token)))
        assert row.token != token and row.used_at


def test_telegram_link_expiry_redirect_validation_and_atomic_consumption():
    storage.ensure_user(702)
    token = web_auth.create_token(702)
    c = web_app.app.test_client()
    assert c.get(f'/login/{token}?next=https://example.org').status_code == 400
    assert c.get(f'/login/{token}?transfer=invalid').status_code == 400
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: web_auth.consume_token(token), range(4)))
    assert results.count(702) == 1 and results.count(None) == 3
    expired = web_auth.create_token(702, minutes=-1)
    assert c.get('/login/' + expired).status_code == 410


@pytest.mark.asyncio
async def test_bot_payment_menu_creates_private_links_without_ai_charge(bot, update, ctx, raw):
    request = update(tb.LABEL)
    await bot.message(request, ctx)
    reply = request.message.reply_text.call_args
    buttons = reply.kwargs['reply_markup'].inline_keyboard
    assert [row[0].text for row in buttons] == ['Российская карта', 'СБП', 'Перевод на Mastercard']
    assert 'transfer=sbp' in buttons[1][0].url
    path = urlsplit(buttons[1][0].url).path
    assert web_auth.peek_token(path.rsplit('/', 1)[1]) == 100
    assert not raw.responses.calls
    with storage.SessionLocal() as db: assert not db.scalar(select(b.CreditUsage))
    request.effective_chat.type = 'group'
    request.message.reply_text.reset_mock()
    await bot.payment_command(request, ctx)
    assert 'reply_markup' not in request.message.reply_text.call_args.kwargs


@pytest.mark.asyncio
async def test_sbp_package_bought_from_bot_is_used_by_bot_and_website(bot, update, ctx):
    mid = setup_sbp()
    request = update('Что такое УЗИ?'); request.message.message_id = 310
    await bot.ensure_current_user(request)
    uid = tb.account_id(100)
    c = web_app.app.test_client()
    assert sign_in(c, web_auth.create_token(100)).status_code == 303
    oid, _ = order_for(c, mid); report(c, oid); b.confirm_order(uid, oid, 123)
    ctx.user_data['dialog_scope'] = 'general'
    await bot.message(request, ctx)
    with storage.SessionLocal() as db:
        assert b.summary(db, uid)['balance'] == 24
        assert db.scalar(select(b.CreditUsage)).status == 'success'
    assert '>24</strong>' in c.get('/billing').text
    assert 'Тестовый ответ' in request.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_telegram_debits_once_replays_and_refunds_provider_failure(bot, update):
    setup_sbp()
    client = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=SimpleNamespace(output_text='Answer'))))
    request = update('Question'); request.message.message_id = 411
    for _ in range(2):
        assert (await tb.generate(request, client, model='test', input=[])).output_text == 'Answer'
    uid = tb.account_id(100)
    assert client.responses.create.call_count == 1
    with storage.SessionLocal() as db: assert b.summary(db, uid)['balance'] == 4
    request.message.message_id = 412
    client.responses.create.side_effect = RuntimeError('provider failed')
    with pytest.raises(RuntimeError): await tb.generate(request, client, model='test', input=[])
    with storage.SessionLocal() as db:
        assert b.summary(db, uid)['balance'] == 4
        assert db.scalar(select(b.CreditUsage).where(b.CreditUsage.request_key == 'tg-100-412')).status == 'failed'


@pytest.mark.asyncio
async def test_insufficient_balance_blocks_provider_and_pdf_limits_do_not_charge(bot, update):
    setup_sbp()
    client = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=SimpleNamespace(output_text='Results'))))
    request = update('Document'); request.message.message_id = 511
    assert await tb.generate(request, client, kind='document', model='test', input=[])
    request.message.message_id = 512
    assert await tb.generate(request, client, model='test', input=[]) is None
    assert client.responses.create.call_count == 1
    assert 'Недостаточно баллов' in request.message.reply_text.call_args.args[0]
    assert request.message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard
    data = io.BytesIO(); pdf = PdfWriter()
    for _ in range(6): pdf.add_blank_page(width=200, height=200)
    pdf.write(data)
    with pytest.raises(b.BillingError): tb.validate_paid_pdf(100, data.getvalue())
    with storage.SessionLocal() as db: assert b.summary(db, tb.account_id(100))['balance'] == 0


@pytest.mark.asyncio
async def test_telegram_photo_analysis_uses_document_paywall_and_cannot_bypass_zero_balance(bot, update, ctx, raw):
    setup_sbp()
    image = Image.new('RGB', (40, 40), 'white')
    buf = io.BytesIO(); image.save(buf, format='JPEG'); payload = buf.getvalue()
    ctx.bot.get_file = AsyncMock(return_value=SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(payload))))

    first = update(photo=[SimpleNamespace(file_id='photo-1', file_unique_id='unique-photo-1', file_size=len(payload))])
    first.message.message_id = 601
    await bot.media(first, ctx)
    uid = tb.account_id(100)
    with storage.SessionLocal() as db:
        usages = db.scalars(select(b.CreditUsage).where(b.CreditUsage.user_id == uid)).all()
        assert len(usages) == 1 and usages[0].kind == 'document' and usages[0].credits == 5
        assert b.summary(db, uid)['balance'] == 0

    second = update(photo=[SimpleNamespace(file_id='photo-2', file_unique_id='unique-photo-2', file_size=len(payload))])
    second.message.message_id = 602
    await bot.media(second, ctx)
    assert any('Недостаточно баллов' in str(call.args[0]) for call in second.message.reply_text.call_args_list)
    with storage.SessionLocal() as db:
        assert len(db.scalars(select(b.CreditUsage).where(b.CreditUsage.user_id == uid)).all()) == 1


@pytest.mark.asyncio
async def test_navigation_and_pet_details_remain_free_after_payments_enabled(bot, update, ctx, raw):
    setup_sbp()
    await bot.message(update('🐾 Мои питомцы'), ctx)
    assert not raw.responses.calls
    with storage.SessionLocal() as db: assert not db.scalar(select(b.CreditUsage))
