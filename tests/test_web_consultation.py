import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
import storage
with patch('openai.OpenAI'):
    import web_app
from consultation_delivery import WebConsultationDelivery, deliver_pending, claim_next
from test_web_app import reset_db, register


def form_client(email='booking@example.com'):
    client=web_app.app.test_client()
    register(client,email)
    html=client.get('/consultation').get_data(as_text=True)
    token=re.search(r'name="request_key" value="([^"]+)"',html).group(1)
    return client,dict(contact='owner@example.com',format='Переписка',question='Хромота после прогулки',pet_summary='Гром, собака 5 лет, 50 кг',request_key=token)


def test_how_it_works_opens_real_public_page():
    c=web_app.app.test_client()
    assert 'href="/how-it-works"' in c.get('/').get_data(as_text=True)
    page=c.get('/how-it-works')
    assert page.status_code==200
    assert 'Создайте анкету питомца' in page.get_data(as_text=True)
    assert 'Запишитесь к ветеринарному врачу' in page.get_data(as_text=True)


def test_booking_confirmation_duplicate_submit_and_private_status():
    reset_db();c,data=form_client()
    first=c.post('/consultation',data=data)
    assert first.status_code==303
    location=first.headers['Location']
    assert location.startswith('/consultation/requests/')
    page=c.get(location).get_data(as_text=True)
    assert 'Заявка №' in page and 'сохранена' in page and data['question'] in page
    assert c.post('/consultation',data=data).headers['Location']==location
    with storage.SessionLocal() as db:
        assert len(db.scalars(select(WebConsultationDelivery)).all())==1
        assert len(db.scalars(select(storage.Consultation)).all())==1
    assert c.get(location+'?status=1').json['state']=='pending'
    other,_=form_client('another@example.com')
    assert other.get(location).status_code==404
    assert other.get(location+'?status=1').status_code==404


def test_booking_validation_keeps_entered_text():
    reset_db();c,data=form_client()
    response=c.post('/consultation',data={**data,'contact':'x'*201})
    assert response.status_code==400
    html=response.get_data(as_text=True)
    assert 'Контакт — до 200 символов' in html
    assert data['question'] in html
    response=c.post('/consultation',data={**data,'request_key':'expired'})
    assert response.status_code==400 and 'Форма устарела' in response.get_data(as_text=True)
    with storage.SessionLocal() as db:assert not db.scalar(select(WebConsultationDelivery))


@pytest.mark.asyncio
async def test_web_request_reaches_configured_telegram_chat_once():
    reset_db();c,data=form_client();location=c.post('/consultation',data=data).headers['Location']
    storage.set_bot_setting('admin_notification_chat_id','-123456')
    application=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(),send_document=AsyncMock()))
    await deliver_pending(application)
    assert application.bot.send_message.await_count==1
    sent=application.bot.send_message.call_args.kwargs
    assert sent['chat_id']==-123456
    assert data['contact'] in sent['text'] and data['question'] in sent['text']
    assert c.get(location+'?status=1').json['state']=='delivered'
    assert 'передана врачу' in c.get(location).get_data(as_text=True)
    await deliver_pending(application)
    assert application.bot.send_message.await_count==1


@pytest.mark.asyncio
async def test_delivery_failure_is_visible_and_retry_preserves_request():
    reset_db();c,data=form_client();location=c.post('/consultation',data=data).headers['Location']
    application=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('offline'))))
    await deliver_pending(application)
    assert c.get(location+'?status=1').json['state']=='retry'
    with storage.SessionLocal() as db:
        item=db.scalar(select(WebConsultationDelivery))
        item.next_attempt=datetime.utcnow()-timedelta(seconds=1);db.commit()
    application.bot.send_message=AsyncMock()
    await deliver_pending(application)
    assert c.get(location+'?status=1').json['state']=='delivered'


@pytest.mark.asyncio
async def test_long_request_delivers_full_text_as_document():
    reset_db();c,data=form_client();data['question']='Описание '*500
    assert c.post('/consultation',data=data).status_code==303
    application=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(),send_document=AsyncMock()))
    await deliver_pending(application)
    assert application.bot.send_document.await_count==1
    payload=application.bot.send_document.call_args.kwargs['document'].getvalue().decode()
    assert data['question'].strip() in payload


def test_claim_lease_prevents_second_worker_and_recovers_abandoned_request():
    reset_db();c,data=form_client();c.post('/consultation',data=data)
    claimed=claim_next();assert claimed
    assert claim_next() is None
    with storage.SessionLocal() as db:
        item=db.get(WebConsultationDelivery,claimed[0]);item.next_attempt=datetime.utcnow()-timedelta(seconds=1);db.commit()
    assert claim_next()==claimed
