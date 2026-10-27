"""Regression cases from the three synthetic production consultations."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import select

import billing
import knowledge
import storage
import telegram_billing
from safety_notice import free_safety_notice
from test_web_app import web_app, reset_db, register
from test_telegram_billing import setup_sbp

ITCH = ('Собака, 3 года, 15 кг. Три недели чешет основание хвоста и задние бедра, '
        'шерсть поредела, есть небольшие корочки. Ест, гуляет и играет как обычно, рвоты нет. '
        'От блох последний раз обрабатывали 5 месяцев назад, самих блох не вижу. '
        'Дома ещё кошка. Это точно аллергия на корм? Хочу сразу перевести на гипоаллергенный '
        'и дать преднизолон. Что лучше сделать?')
URINARY = 'Кот не может помочиться, кричит, мочи нет совсем. Что делать сейчас?'
EYE = 'Бульдог щурит один глаз после прогулки, на роговице мутное пятно. Можно Тобрадекс?'


def protocols(text):
    return {p['title'] for p in knowledge.match_protocols(text)}


def exhaust(uid):
    with storage.SessionLocal() as db:
        billing.trial(db, uid)
        for grant in db.scalars(select(billing.CreditGrant).where(billing.CreditGrant.user_id == uid)):
            grant.remaining = 0
        db.commit()


def assert_no_usage(uid):
    with storage.SessionLocal() as db:
        assert billing.summary(db, uid)['balance'] == 0
        assert not db.scalar(select(billing.CreditUsage).where(billing.CreditUsage.user_id == uid))


def test_live_itch_case_selects_skin_not_denied_gastrointestinal_symptoms():
    selected = protocols(ITCH)
    assert 'Зуд и дерматологические жалобы у собак и кошек' in selected
    assert not any('рвота' in p.lower() or 'отказ от еды' in p.lower() for p in selected)


@pytest.mark.parametrize('denial', [
    'Рвоты нет', 'Нет рвоты', 'Без рвоты', 'Рвоты не было', 'Рвота отсутствует',
    'Не было рвоты', 'Не наблюдается рвоты', 'Нет рвоты и поноса',
    'Рвоты, поноса нет', 'Не рвёт', 'Уже не рвёт', 'No vomiting', 'Without vomiting',
])
def test_denied_vomiting_does_not_retrieve_vomiting_protocol(denial):
    selected = protocols('Собака чешет бок. ' + denial)
    assert not any('рвота' in p.lower() for p in selected)
    assert 'Зуд и дерматологические жалобы у собак и кошек' in selected


@pytest.mark.parametrize('text', [
    'Рвоты нет, но собаку снова вырвало',
    'Нет поноса. Собаку рвёт',
    'Собака ест нормально. Рвота после еды',
])
def test_positive_symptom_is_not_erased_by_a_separate_denial(text):
    assert any('рвота' in p.lower() for p in protocols(text))


def test_negative_inability_is_retained_and_unrelated_negative_not_joined():
    assert any('отказ от еды' in p.lower() for p in protocols('Собака не ест'))
    assert any('обструкция' in p.lower() for p in protocols('Кот не может помочиться'))
    assert not any('отказ от еды' in p.lower() for p in protocols('Собака ест нормально, блох не вижу'))


@pytest.mark.parametrize('text,expected', [(URINARY, 'немедленно'), (EYE, 'сегодня'),
    ('Кот мочи не видим, не писает с утра', 'немедленно'),
    ('Кошка задыхается', 'сейчас'), ('Кролик не ест и вздулся', 'немедлен'),
    ('My cat cannot urinate', 'now')])
def test_free_warning_covers_specific_red_flags(text, expected):
    assert expected in free_safety_notice(text).lower()


@pytest.mark.parametrize('text', [
    ITCH, 'Кот писает нормально. Рвоты нет', 'Собака не щурит глаз и не задыхается',
    'Что такое уретральная обструкция?', 'Расскажи, почему кот не может помочиться',
    'Год назад кот не писает — так было записано в карте. Сейчас здоров',
])
def test_free_warning_does_not_assert_a_current_emergency_for_benign_or_educational_text(text):
    assert free_safety_notice(text) == ''


@pytest.mark.parametrize('question', [URINARY, EYE])
def test_zero_balance_web_shows_free_safety_without_model_or_debit(question):
    reset_db(); setup_sbp(); client = web_app.app.test_client(); register(client)
    with client.session_transaction() as state: uid = state['uid']
    exhaust(uid)
    with patch.object(web_app.client.responses, 'create') as model:
        for _ in range(2):
            result = client.post('/api/chat', json={'message':question, 'request_key':'free-safety-repeat'})
            assert result.status_code == 402
            assert result.json['free_safety_notice'] is True
            assert result.json['answer'] == free_safety_notice(question, 'ru')
        model.assert_not_called()
    assert_no_usage(uid)


def test_zero_balance_normal_question_still_uses_regular_payment_error():
    reset_db(); setup_sbp(); client = web_app.app.test_client(); register(client)
    with client.session_transaction() as state: uid = state['uid']
    exhaust(uid)
    with patch.object(web_app.client.responses, 'create') as model:
        result = client.post('/api/chat', json={'message':'Что такое УЗИ?'})
        assert result.status_code == 402 and 'free_safety_notice' not in result.json
        model.assert_not_called()


def test_safety_notice_respects_english_interface():
    reset_db(); setup_sbp(); client = web_app.app.test_client(); register(client)
    client.set_cookie('language','en')
    with client.session_transaction() as state: uid = state['uid']
    exhaust(uid)
    result = client.post('/api/chat', json={'message':URINARY})
    assert result.json['answer'].startswith('Free urgent-care guidance')


def test_funded_request_keeps_full_answer_and_free_replay_after_last_credit():
    reset_db(); setup_sbp(); client = web_app.app.test_client(); register(client)
    with client.session_transaction() as state: uid = state['uid']
    exhaust(uid)
    with storage.SessionLocal() as db:
        grant = db.scalar(select(billing.CreditGrant).where(billing.CreditGrant.user_id == uid))
        grant.remaining = 1; db.commit()
    with patch.object(web_app.client.responses, 'create', return_value=SimpleNamespace(output_text='Full clinical answer', model='test')) as model:
        for _ in range(2):
            result = client.post('/api/chat', json={'message':URINARY, 'request_key':'funded-safety-repeat'})
            assert result.status_code == 200 and result.json['answer'] == 'Full clinical answer'
        assert model.call_count == 1
    with storage.SessionLocal() as db: assert billing.summary(db, uid)['balance'] == 0


@pytest.mark.parametrize('question', [URINARY, EYE])
async def test_zero_balance_telegram_real_handler_sends_notice_not_checkout(bot, ctx, update, raw, question):
    setup_sbp(); storage.ensure_user(100)
    uid = telegram_billing.account_id(100); exhaust(uid)
    ctx.user_data['dialog_scope'] = 'general'
    for _ in range(2):
        incoming = update(question)
        await bot.message(incoming, ctx)
        replies = '\n'.join(str(c.args[0]) for c in incoming.effective_message.reply_text.call_args_list if c.args)
        assert free_safety_notice(question) in replies
        assert 'Выберите способ оплаты' not in replies
    assert raw.responses.calls == []
    assert_no_usage(uid)


async def test_voice_transcript_is_available_to_free_safety_notice(bot, update):
    setup_sbp(); storage.ensure_user(100)
    uid = telegram_billing.account_id(100); exhaust(uid)
    ai = SimpleNamespace(responses=SimpleNamespace(create=Mock()))
    incoming = update(voice=SimpleNamespace(file_id='synthetic',file_unique_id='synthetic'))
    result = await telegram_billing.generate(incoming, ai, input=[{'role':'user','content':URINARY}])
    assert result is None
    assert free_safety_notice(URINARY) in incoming.effective_message.reply_text.call_args.args[0]
    ai.responses.create.assert_not_called()
    assert_no_usage(uid)
