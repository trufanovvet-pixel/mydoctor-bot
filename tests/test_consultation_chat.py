import io
import uuid
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import pytest
from PIL import Image
from sqlalchemy import select,func
import storage
with patch('openai.OpenAI'):
    import web_app
from consultation_chat import ConsultationMessage,MessageFile,MessageNotification,ConversationRead,deliver_message_notifications,claim_message_notification
from test_web_app import reset_db,register
from test_web_consultation import form_client
from test_doctor_portal import doctor,field,request_with_pet_and_documents


def setup():
    reset_db();owner,uid,rid,_=request_with_pet_and_documents();staff=doctor()
    return owner,uid,rid,staff


def headers(client,rid,role='owner'):
    page=client.get(f'/doctor/requests/{rid}' if role=='doctor' else f'/consultation/requests/{rid}').get_data(as_text=True)
    return {'X-Chat-Request':'1','X-Message-CSRF':field(page,'message_csrf')}


def send(client,rid,text,role='owner',extra=None,key=None):
    prefix='doctor' if role=='doctor' else 'consultation'
    return client.post(f'/{prefix}/requests/{rid}/messages',data={
        'body':text,'request_key':key or str(uuid.uuid4()),**(extra or {})},headers=headers(client,rid,role))


def test_owner_and_doctor_exchange_messages_files_and_read_receipts():
    owner,uid,rid,staff=setup()
    key=str(uuid.uuid4())
    first=send(owner,rid,'Could you review this?',extra={'files':(io.BytesIO(b'%PDF-test'),'cbc.pdf')},key=key)
    assert first.status_code==200
    message=first.json['message'];mid=message['id'];url=message['files'][0]['url']
    assert send(owner,rid,'Could you review this?',key=key).json['message']['id']==mid
    with storage.SessionLocal() as db:
        assert db.scalar(select(func.count(ConsultationMessage.id)))==1
        assert db.scalar(select(func.count(MessageNotification.message_id)))==1
    assert owner.get(url).data==b'%PDF-test' and staff.get(url).data==b'%PDF-test'
    assert staff.get('/messages/unread?role=doctor').json['count']==1
    # Fetching the message is not itself proof that the doctor read it.
    assert staff.get(f'/doctor/requests/{rid}/messages').json['messages'][0]['body']=='Could you review this?'
    assert staff.get('/messages/unread?role=doctor').json['count']==1
    assert staff.post(f'/doctor/requests/{rid}/messages/read',data={'last_id':mid},headers=headers(staff,rid,'doctor')).status_code==200
    assert staff.get('/messages/unread?role=doctor').json['count']==0
    assert owner.get(f'/consultation/requests/{rid}/messages').json['other_seen']==mid
    reply=send(staff,rid,'I have reviewed the document.',role='doctor');assert reply.status_code==200
    assert owner.get('/messages/unread').json['count']==1
    assert 'I have reviewed the document.' in owner.get(f'/consultation/requests/{rid}').get_data(as_text=True)
    assert owner.post(f'/consultation/requests/{rid}/messages/read',data={'last_id':reply.json['message']['id']},headers=headers(owner,rid)).status_code==200
    assert owner.get('/messages/unread').json['count']==0
    with storage.SessionLocal() as db:assert db.scalar(select(func.count(MessageNotification.message_id)))==1


def test_private_messages_attachments_and_doctor_notes_are_isolated():
    owner,uid,rid,staff=setup();other,_=form_client('stranger@example.com')
    result=send(staff,rid,'Visible reply',role='doctor',extra={'files':(io.BytesIO(b'%PDF-owner-only'),'recommendations.pdf')})
    file_url=result.json['message']['files'][0]['url']
    anon=web_app.app.test_client()
    assert other.get(f'/consultation/requests/{rid}/messages').status_code==404
    assert anon.get(f'/consultation/requests/{rid}/messages').status_code==401
    assert other.get(file_url).status_code==404 and anon.get(file_url).status_code==401
    assert other.post(f'/doctor/requests/{rid}/messages',data={'body':'Forged doctor'}).status_code==403
    assert owner.get('/messages/unread?role=doctor').status_code==403
    assert other.get('/messages/unread').json['count']==0
    assert f'Заявка №{rid}' not in other.get('/messages').get_data(as_text=True)
    assert owner.post(f'/consultation/requests/{rid}/messages',data={'body':'No CSRF','request_key':str(uuid.uuid4())}).status_code==400
    assert other.post(f'/consultation/requests/{rid}/messages/read',data={'last_id':result.json['message']['id']}).status_code==404
    from consultation_cases import ConsultationCase
    with storage.SessionLocal() as db:
        db.get(ConsultationCase,rid).doctor_notes='Private staff note';db.commit()
    assert 'Private staff note' not in str(owner.get(f'/consultation/requests/{rid}/messages').json)
    assert 'Private staff note' not in owner.get(f'/consultation/requests/{rid}').get_data(as_text=True)


def test_message_validation_is_atomic_and_images_are_normalized():
    owner,uid,rid,staff=setup()
    for text,extra in [('',{}),('x'*10001,{}),('File',{'files':(io.BytesIO(b'<script>alert(1)</script>'),'fake.png')}),
                       ('Files',{'files':[(io.BytesIO(b'%PDF-a'),f'{i}.pdf') for i in range(6)]})]:
        assert send(owner,rid,text,extra=extra).status_code==400
    assert send(owner,rid,'Bad key',key='invalid').status_code==400
    with storage.SessionLocal() as db:
        assert not db.scalar(select(ConsultationMessage))
        assert not db.scalar(select(MessageFile))
    png=io.BytesIO();Image.new('RGB',(20,20),'white').save(png,'PNG');png.seek(0)
    result=send(owner,rid,'<script>alert(1)</script>',extra={'files':(png,'photo.png')})
    url=result.json['message']['files'][0]['url'];download=owner.get(url)
    assert download.mimetype=='image/jpeg' and download.data.startswith(b'\xff\xd8')
    assert 'attachment;' in download.headers['Content-Disposition'] and download.headers['X-Content-Type-Options']=='nosniff'
    page=owner.get(f'/consultation/requests/{rid}').get_data(as_text=True)
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page
    assert '<script>alert(1)</script>' not in page


def test_history_pagination_and_read_position_cannot_cross_threads_or_rewind():
    owner,uid,rid,staff=setup()
    with storage.SessionLocal() as db:
        rows=[ConsultationMessage(consultation_id=rid,role='doctor',actor_id=1,request_key=str(uuid.uuid4()),body=f'Message {i}') for i in range(125)]
        db.add_all(rows);db.commit();ids=[row.id for row in rows]
    first=owner.get(f'/consultation/requests/{rid}/messages').json
    assert len(first['messages'])==50 and first['more']
    second=owner.get(f'/consultation/requests/{rid}/messages?after='+str(first['messages'][-1]['id'])).json
    assert second['messages'][0]['id']==ids[50]
    before=owner.get(f'/consultation/requests/{rid}/messages?before='+str(ids[75])).json
    assert before['messages'][0]['id']==ids[25] and before['messages'][-1]['id']==ids[74]
    html=owner.get(f'/consultation/requests/{rid}').get_data(as_text=True)
    assert f'data-message-id="{ids[75]}"' in html and f'data-message-id="{ids[0]}"' not in html
    h=headers(owner,rid)
    for mid in (ids[-1],ids[0]):assert owner.post(f'/consultation/requests/{rid}/messages/read',data={'last_id':mid},headers=h).status_code==200
    with storage.SessionLocal() as db:assert db.get(ConversationRead,rid).owner_seen==ids[-1]
    assert owner.post(f'/consultation/requests/{rid}/messages/read',data={'last_id':999999},headers=h).status_code==400


@pytest.mark.asyncio
async def test_notifications_retry_without_losing_or_resending_saved_messages():
    owner,uid,rid,staff=setup();send(owner,rid,'Private clinical detail')
    storage.set_bot_setting('admin_notification_chat_id','-123456')
    application=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('offline'))))
    await deliver_message_notifications(application)
    with storage.SessionLocal() as db:
        item=db.scalar(select(MessageNotification));assert item.state=='retry'
        item.next_attempt=datetime.utcnow()-timedelta(seconds=1);db.commit()
    application.bot.send_message=AsyncMock();await deliver_message_notifications(application)
    payload=application.bot.send_message.call_args.kwargs
    assert payload['chat_id']==-123456 and 'Private clinical detail' not in payload['text']
    assert payload['reply_markup'].inline_keyboard[0][0].url.endswith(f'/doctor/requests/{rid}#conversation')
    await deliver_message_notifications(application);assert application.bot.send_message.await_count==1
    assert owner.get(f'/consultation/requests/{rid}/messages').json['messages'][0]['body']=='Private clinical detail'


def test_client_record_contains_submitted_history_not_private_ai_or_unshared_files():
    owner,uid,rid,staff=setup()
    send(owner,rid,'Owner message',extra={'files':(io.BytesIO(b'%PDF-chat'),'message-document.pdf')})
    with storage.SessionLocal() as db:
        db.add(storage.Consultation(user_id=uid,kind='web_chat',user_text='Unrelated AI conversation',assistant_text='Private answer'))
        db.add(web_app.WebDocument(user_id=uid,filename='not-shared.pdf',mime_type='application/pdf',data=b'%PDF-private'))
        db.add(storage.Pet(user_id=uid,name='NeverSharedPet',species='Кошка'))
        db.commit()
    html=staff.get(f'/doctor/clients/{uid}').get_data(as_text=True)
    for text in ('cbc.pdf','discharge.pdf','message-document.pdf','Archie'):assert text in html
    for text in ('Unrelated AI conversation','Private answer','not-shared.pdf','NeverSharedPet'):assert text not in html
    assert staff.get('/doctor/clients?q=QA').status_code==200
    assert owner.get(f'/doctor/clients/{uid}').status_code==302
    other,_=form_client('never-submitted@example.com')
    with other.session_transaction() as s:other_uid=s['uid']
    assert staff.get(f'/doctor/clients/{other_uid}').status_code==404


def test_site_only_owner_needs_no_messenger_and_ui_is_translated():
    reset_db();owner,data=form_client();data['contact']=''
    location=owner.post('/consultation',data=data).headers['Location'];rid=int(location.rsplit('/',1)[1])
    assert 'Переписка на сайте' in owner.get(location).get_data(as_text=True)
    profile=owner.get('/profile').get_data(as_text=True)
    assert owner.post('/profile',data={'profile_key':field(profile,'profile_key'),'preferred':'site','name':'Owner'}).status_code==303
    owner.get('/language/en');html=owner.get(location).get_data(as_text=True)
    assert 'Message your veterinarian' in html
    assert 'Telegram and other messengers are not required' in owner.get('/messages').get_data(as_text=True)
    assert send(owner,rid,'Hello').status_code==200
