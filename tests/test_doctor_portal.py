import io
import re
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
import pytest
from sqlalchemy import select
import storage
with patch('openai.OpenAI'):
    import web_app
from doctor_access import create_doctor_link,DoctorAccess,doctor_command,hashed,doctor_start_handler,DOCTOR_COOKIE,REMEMBER_SECONDS
from notification_patch import _admin_user_id
from consultation_cases import ConsultationCase,RequestAttachment,RequestStatusEvent
from consultation_delivery import WebConsultationDelivery
from patient_records import OperationRecord,OperationAttachment
from test_web_app import reset_db,register
from test_web_consultation import form_client


def field(html,name):return re.search('name="'+name+'" value="([^"]*)"',html).group(1)


def doctor():
    client=web_app.app.test_client();token=create_doctor_link(_admin_user_id())
    html=client.get('/doctor/access/'+token).get_data(as_text=True)
    assert client.post('/doctor/access',data={'access_token':token,'doctor_csrf':field(html,'doctor_csrf')}).status_code==303
    return client


def request_with_pet_and_documents():
    owner,data=form_client()
    with owner.session_transaction() as s:uid=s['uid']
    with storage.SessionLocal() as db:
        pet=storage.Pet(user_id=uid,name='Archie',species='Собака',age='5 years',weight_kg=12);db.add(pet);db.flush()
        doc=web_app.WebDocument(user_id=uid,pet_id=pet.id,filename='cbc.pdf',mime_type='application/pdf',data=b'%PDF-cbc',analysis='Review');db.add(doc)
        op=OperationRecord(user_id=uid,pet_id=pet.id,title='TPLO');db.add(op);db.flush()
        discharge=OperationAttachment(user_id=uid,operation_id=op.id,filename='discharge.pdf',mime_type='application/pdf',data=b'%PDF-discharge');db.add(discharge);db.commit()
        pid,did,oid=pet.id,doc.id,discharge.id
    data.update(pet_id=pid,consultation_type='followup',symptoms_since='Three days',treatment='Rest',documents=[f'web:{did}',f'operation:{oid}'])
    location=owner.post('/consultation',data=data).headers['Location'];rid=int(location.rsplit('/',1)[1])
    return owner,uid,rid,data


def test_expanded_request_preserves_patient_and_attached_documents():
    reset_db();owner,uid,rid,data=request_with_pet_and_documents()
    with storage.SessionLocal() as db:
        case=db.get(ConsultationCase,rid);record=db.get(storage.Consultation,rid)
        assert case.consultation_type=='followup' and case.patient['name']=='Archie'
        assert 'Three days' in record.user_text and 'cbc.pdf' in record.user_text
        assert record.pet_id==data['pet_id']
        db.get(storage.Pet,data['pet_id']).name='Updated name';db.commit()
        assert db.get(ConsultationCase,rid).patient['name']=='Archie'
        files=db.scalars(select(RequestAttachment).where(RequestAttachment.consultation_id==rid)).all()
    other,_=form_client('other@example.com');staff=doctor()
    for file in files:
        assert owner.get(f'/consultation/files/{file.id}').status_code==200
        assert staff.get(f'/consultation/files/{file.id}').data.startswith(b'%PDF-')
        assert other.get(f'/consultation/files/{file.id}').status_code==404
        assert web_app.app.test_client().get(f'/consultation/files/{file.id}').status_code==401
    assert owner.post('/consultation',data=data).headers['Location'].endswith('/'+str(rid))
    with storage.SessionLocal() as db:assert len(db.scalars(select(RequestAttachment)).all())==2


def test_owner_cannot_attach_other_users_documents_or_open_doctor_pages():
    reset_db();owner,uid,rid,data=request_with_pet_and_documents()
    outsider,foreign=form_client('outsider@example.com')
    for forged in [{'pet_id':data['pet_id']},{'documents':data['documents']},{'documents':['web:bad']},{'consultation_type':'fake'}]:
        assert outsider.post('/consultation',data={**foreign,**forged}).status_code==400
    assert owner.get('/doctor').headers['Location'].endswith('/doctor/login')
    assert owner.get(f'/doctor/requests/{rid}').status_code==302
    assert owner.post(f'/doctor/requests/{rid}',data={'status':'confirmed','paid_confirmed':'1'}).status_code==403
    with pytest.raises(PermissionError):create_doctor_link(999999)


def test_doctor_workflow_payment_gate_private_notes_and_stale_updates():
    reset_db();owner,uid,rid,_=request_with_pet_and_documents();staff=doctor()
    assert staff.get('/doctor').status_code==200
    page=staff.get(f'/doctor/requests/{rid}').get_data(as_text=True)
    values={'doctor_csrf':field(page,'doctor_csrf'),'version':'0','status':'confirmed','doctor_notes':'Private clinical note'}
    assert staff.post(f'/doctor/requests/{rid}',data=values).status_code==400
    assert staff.post(f'/doctor/requests/{rid}',data={**values,'paid_confirmed':'1'}).status_code==303
    assert staff.post(f'/doctor/requests/{rid}',data={**values,'paid_confirmed':'1'}).status_code==409
    assert staff.post(f'/doctor/requests/{rid}',data={**values,'version':'1','doctor_csrf':'wrong'}).status_code==400
    html=owner.get(f'/consultation/requests/{rid}').get_data(as_text=True)
    assert 'Запись подтверждена' in html and 'Private clinical note' not in html
    assert 'Private clinical note' not in str(owner.get(f'/consultation/requests/{rid}?status=1').json)
    assert 'Private clinical note' not in owner.get('/history').get_data(as_text=True)
    assert 'Archie' in staff.get('/doctor?status=confirmed&q=cbc').get_data(as_text=True)
    assert f'/doctor/requests/{rid}' not in staff.get('/doctor?status=new').get_data(as_text=True)
    with storage.SessionLocal() as db:
        assert len(db.scalars(select(RequestStatusEvent)).all())==1
        assert db.get(ConsultationCase,rid).paid_confirmed


def test_doctor_link_is_single_use_expires_and_session_can_be_revoked():
    reset_db();client=web_app.app.test_client();token=create_doctor_link(_admin_user_id())
    first=client.get('/doctor/access/'+token);assert first.status_code==200
    client.get('/doctor/access/'+token)
    key=field(first.get_data(as_text=True),'doctor_csrf')
    assert client.post('/doctor/access',data={'access_token':token,'doctor_csrf':'bad'}).status_code==400
    assert client.post('/doctor/access',data={'access_token':token,'doctor_csrf':key}).status_code==303
    page=client.get('/doctor').get_data(as_text=True);key=field(page,'doctor_csrf')
    assert client.post('/doctor/access',data={'access_token':token,'doctor_csrf':key}).status_code==400
    with client.session_transaction() as s:grant=s['doctor_grant']
    assert client.post('/doctor/logout',data={'doctor_csrf':key}).status_code==303
    with client.session_transaction() as s:s['doctor_grant']=grant
    assert client.get('/doctor').status_code==302
    expired=create_doctor_link(_admin_user_id())
    with storage.SessionLocal() as db:
        db.get(DoctorAccess,hashed(expired)).expires_at=datetime.utcnow()-timedelta(seconds=1);db.commit()
    page=client.get('/doctor/access/'+expired).get_data(as_text=True)
    assert client.post('/doctor/access',data={'access_token':expired,'doctor_csrf':field(page,'doctor_csrf')}).status_code==400


@pytest.mark.asyncio
async def test_doctor_command_is_private_and_admin_only(update,ctx):
    reset_db();outsider=update('/doctor',user_id=999999)
    await doctor_command(outsider,ctx)
    assert 'администратору' in outsider.message.reply_text.call_args.args[0]
    admin=update('/doctor',user_id=_admin_user_id());admin.effective_chat.type='group'
    await doctor_command(admin,ctx)
    assert 'личный чат' in admin.message.reply_text.call_args.args[0]
    with storage.SessionLocal() as db:assert not db.scalar(select(DoctorAccess))
    admin.effective_chat.type='private';await doctor_command(admin,ctx)
    button=admin.message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert '/doctor/access/' in button.url


def test_legacy_telegram_requests_are_manageable_without_importing_private_chat():
    reset_db();owner,_=form_client()
    with owner.session_transaction() as s:uid=s['uid']
    with storage.SessionLocal() as db:
        old=storage.Consultation(user_id=uid,kind='consult_request',user_text='Telegram request',assistant_text='Saved')
        db.add(old);db.add(storage.Consultation(user_id=uid,kind='web_chat',user_text='Unrelated private chat',assistant_text='Answer'));db.commit();rid=old.id
    staff=doctor();html=staff.get('/doctor').get_data(as_text=True)
    assert f'/doctor/requests/{rid}' in html and 'Unrelated private chat' not in html
    page=staff.get(f'/doctor/requests/{rid}').get_data(as_text=True)
    assert staff.post(f'/doctor/requests/{rid}',data={'doctor_csrf':field(page,'doctor_csrf'),'version':'0','status':'contacting'}).status_code==303
    with storage.SessionLocal() as db:assert db.get(ConsultationCase,rid).status=='contacting'


@pytest.mark.asyncio
async def test_telegram_login_button_returns_to_request_in_a_new_browser(update,ctx):
    from urllib.parse import urlsplit,parse_qs
    reset_db();_,_,rid,_=request_with_pet_and_documents()
    storage.set_bot_setting('doctor_bot_username','MyDoctorTestBot')
    original=web_app.app.test_client()
    page=original.get(f'/doctor/requests/{rid}',follow_redirects=True).get_data(as_text=True)
    assert f'https://t.me/MyDoctorTestBot?start=doctor_{rid}' in page
    assert 'Войти через Telegram' in page and '<code class="doctor-command">' not in page
    handler=doctor_start_handler(AsyncMock())
    ctx.args=[f'doctor_{rid}']
    outsider=update('/start',user_id=999999)
    await handler(outsider,ctx)
    assert 'администратору' in outsider.message.reply_text.call_args.args[0]
    with storage.SessionLocal() as db:assert not db.scalar(select(DoctorAccess))
    admin=update('/start',user_id=_admin_user_id())
    await handler(admin,ctx)
    button=admin.message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
    url=urlsplit(button.url)
    assert parse_qs(url.query)['next']==[f'/doctor/requests/{rid}']
    fresh=web_app.app.test_client()
    page=fresh.get(url.path+'?'+url.query).get_data(as_text=True)
    response=fresh.post('/doctor/access',data={'doctor_csrf':field(page,'doctor_csrf'),'access_token':field(page,'access_token'),'next':field(page,'next'),'remember':'1'})
    assert response.headers['Location']==f'/doctor/requests/{rid}'
    cookie=next(value for value in response.headers.getlist('Set-Cookie') if value.startswith(DOCTOR_COOKIE+'='))
    assert f'Max-Age={REMEMBER_SECONDS}' in cookie and 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie
    fresh.delete_cookie(web_app.app.config['SESSION_COOKIE_NAME'])
    assert fresh.get(f'/doctor/requests/{rid}').status_code==200
    with storage.SessionLocal() as db:
        grant=db.scalar(select(DoctorAccess))
        assert timedelta(days=29)<grant.session_expires_at-datetime.utcnow()<=timedelta(days=30)
    page=fresh.get('/doctor').get_data(as_text=True)
    stolen=fresh.get_cookie(DOCTOR_COOKIE).value
    assert fresh.post('/doctor/logout',data={'doctor_csrf':field(page,'doctor_csrf')}).status_code==303
    assert fresh.get_cookie(DOCTOR_COOKIE) is None
    fresh.set_cookie(DOCTOR_COOKIE,stolen)
    assert fresh.get('/doctor').status_code==302


def test_login_targets_are_local_expired_links_have_retry_and_site_logout_revokes():
    reset_db();storage.set_bot_setting('doctor_bot_username','MyDoctorTestBot')
    for target in ['//example.com','/doctor.evil.com','/doctor/../logout','/doctor\\evil','https://example.com']:
        client=web_app.app.test_client();token=create_doctor_link(_admin_user_id())
        page=client.get('/doctor/access/'+token,query_string={'next':target}).get_data(as_text=True)
        response=client.post('/doctor/access',data={'doctor_csrf':field(page,'doctor_csrf'),'access_token':token,'next':target})
        assert response.headers['Location']=='/doctor'
        cookie=next(value for value in response.headers.getlist('Set-Cookie') if value.startswith(DOCTOR_COOKIE+'='))
        assert 'Max-Age' not in cookie
        with storage.SessionLocal() as db:
            assert db.get(DoctorAccess,hashed(token)).session_expires_at<datetime.utcnow()+timedelta(hours=9)
        saved=client.get_cookie(DOCTOR_COOKIE).value
        client.get('/logout');client.set_cookie(DOCTOR_COOKIE,saved)
        assert client.get('/doctor').status_code==302
    client=web_app.app.test_client()
    page=client.get('/doctor/access/expired?next=/doctor/requests/114').get_data(as_text=True)
    result=client.post('/doctor/access',data={'doctor_csrf':field(page,'doctor_csrf'),'access_token':'expired','next':'/doctor/requests/114'})
    assert result.status_code==400
    assert 'https://t.me/MyDoctorTestBot?start=doctor_114' in result.get_data(as_text=True)
    assert client.get_cookie(DOCTOR_COOKIE) is None


@pytest.mark.asyncio
async def test_doctor_deep_link_does_not_break_normal_start_or_leak_in_groups(update,ctx):
    reset_db();normal=AsyncMock();handler=doctor_start_handler(normal)
    ctx.args=[];await handler(update('/start'),ctx);normal.assert_awaited_once()
    ctx.args=['doctor'];admin=update('/start',user_id=_admin_user_id());admin.effective_chat.type='group'
    await handler(admin,ctx)
    assert 'личный чат' in admin.message.reply_text.call_args.args[0]
    with storage.SessionLocal() as db:assert not db.scalar(select(DoctorAccess))
    ctx.args=['doctor_999999'];admin.effective_chat.type='private'
    await handler(admin,ctx)
    assert 'не найдена' in admin.message.reply_text.call_args.args[0]
    with storage.SessionLocal() as db:assert not db.scalar(select(DoctorAccess))
