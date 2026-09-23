import io
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import pytest
from sqlalchemy import select
import storage
with patch("openai.OpenAI"):
    import web_app
from patient_records import PetPhoto, OperationRecord, OperationAttachment, DocumentLabel, plain_text, classify_document
from test_web_app import reset_db, register


def account(email='section@example.com'):
    c=web_app.app.test_client();register(c,email)
    with c.session_transaction() as s:uid=s['uid']
    c.post('/pets',data={'name':'Гром','species':'Собака','weight':'50'})
    with storage.SessionLocal() as db:pid=db.scalar(select(storage.Pet.id).where(storage.Pet.user_id==uid))
    return c,uid,pid


def test_separate_pages_and_closed_archive():
    reset_db();c,uid,pid=account()
    with storage.SessionLocal() as db:
        d=web_app.WebDocument(user_id=uid,pet_id=pid,filename='ОАК.pdf',mime_type='application/pdf',data=b'%PDF-',analysis='### Скрытый разбор **анализов**');db.add(d);db.commit();did=d.id
    for path in ['/dashboard','/pets',f'/pets/{pid}','/analyses','/operations','/prevention','/assistant','/consultation']:
        r=c.get(path);assert r.status_code==200,(path,r.status_code)
        html=r.get_data(as_text=True);assert '/app#' not in html
        assert 'Скрытый разбор' not in html
    html=c.get(f'/analyses/{did}').get_data(as_text=True)
    assert 'Скрытый разбор анализов' in html and '###' not in html and '**' not in html
    assert c.get('/app').headers['Location'].endswith('/dashboard')


def test_categories_and_owner_isolation():
    reset_db();a,uid,pid=account();b,_,_=account('other@example.com')
    with patch.object(web_app.client.responses,'create',return_value=SimpleNamespace(output_text='**УЗИ** брюшной полости')):
        r=a.post('/documents',data={'file':(io.BytesIO(b'%PDF-1.4 test'),'исследование.pdf'),'category':'auto','pet_id':str(pid),'study_date':'2026-09-20'},content_type='multipart/form-data')
    assert r.status_code==302
    with storage.SessionLocal() as db:
        d=db.scalar(select(web_app.WebDocument).where(web_app.WebDocument.user_id==uid));did=d.id
        assert d.filename=='исследование.pdf'
        assert db.get(DocumentLabel,('web',did)).category=='ultrasound'
    assert 'исследование.pdf' in a.get('/analyses?category=ultrasound').get_data(as_text=True)
    assert 'исследование.pdf' not in a.get('/analyses?category=cbc').get_data(as_text=True)
    assert b.get(f'/analyses/{did}').status_code==404
    assert b.post(f'/analyses/{did}',data={'category':'cbc'}).status_code==404
    assert b.get(f'/analyses?pet_id={pid}').status_code==404


def test_pet_photo_and_operation_upload_are_private():
    from PIL import Image
    reset_db();a,uid,pid=account();b,_,_=account('other@example.com')
    pic=io.BytesIO();Image.new('RGB',(16,16),'blue').save(pic,format='PNG');pic.seek(0)
    assert a.post(f'/pets/{pid}/photo',data={'photo':(pic,'pet.png')},content_type='multipart/form-data').status_code==302
    assert a.get(f'/pets/{pid}/photo').status_code==200
    assert b.get(f'/pets/{pid}/photo').status_code==404
    assert b.post(f'/pets/{pid}/photo',data={}).status_code==404
    assert a.post('/operations',data={'pet_id':pid,'title':'TPLO','operation_date':'2026-09-10'}).status_code==302
    with storage.SessionLocal() as db:oid=db.scalar(select(OperationRecord.id).where(OperationRecord.user_id==uid))
    assert b.post(f'/operations/{oid}',data={}).status_code==404
    assert a.post(f'/operations/{oid}',data={'file':(io.BytesIO(b'%PDF-1.4 test'),'выписка.pdf')},content_type='multipart/form-data').status_code==302
    with storage.SessionLocal() as db:fid=db.scalar(select(OperationAttachment.id).where(OperationAttachment.user_id==uid))
    assert a.get(f'/operation-files/{fid}').status_code==200
    assert b.get(f'/operation-files/{fid}').status_code==404
    assert b.get(f'/operations/{oid}').status_code==404
    assert b.post('/operations',data={'pet_id':pid,'title':'foreign'}).status_code==404


def test_markdown_old_answers_and_numeric_values():
    assert plain_text('### Итог\n**Ответ**\n* пункт\n*курсив*\n5 * 2 = 10')=='Итог\nОтвет\n• пункт\nкурсив\n5 * 2 = 10'
    reset_db();c,uid,pid=account()
    with storage.SessionLocal() as db:
        db.add(storage.Consultation(user_id=uid,pet_id=None,kind='web_chat',user_text='Вопрос',assistant_text='### Итог\n**Ответ**'));db.commit()
    html=c.get('/assistant').get_data(as_text=True)
    assert '**Ответ**' not in html and '### Итог' not in html and 'Итог\nОтвет' in html
    assert c.post('/pets',data={'name':'Bad','species':'Собака','weight':'NaN'}).status_code==302
    with storage.SessionLocal() as db:assert db.scalar(select(storage.Pet).where(storage.Pet.name=='Bad')) is None


@pytest.mark.parametrize('text,expected',[('УЗИ','ultrasound'),('МРТ головы','mri'),('КТ','ct'),('Общий анализ крови','cbc'),('Биохимия','biochemistry'),('Анализ мочи','urine'),('Рентген','xray')])
def test_category_detection(text,expected):assert classify_document(text)==expected


@pytest.mark.asyncio
async def test_telegram_operation_flow_and_archive(bot,ctx,update,pet):
    import operations_patch
    async def say(text):await bot.message(update(text),ctx)
    await say(operations_patch.OPERATIONS)
    assert ctx.user_data['operation_flow']['step']=='pet'
    await say(next(iter(ctx.user_data['operation_flow']['choices'])))
    await say('➕ Добавить операцию');await say('TPLO');await say('10.09.2026');await say('Без заметки')
    flow=ctx.user_data['operation_flow'];assert flow['step']=='detail'
    oid=flow['operation_id']
    with storage.SessionLocal() as db:
        op=db.get(OperationRecord,oid);assert op.pet_id==pet['id'] and op.title=='TPLO'
    await say('📎 Добавить выписку')
    ctx.bot.get_file=AsyncMock(return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b'%PDF-1.4 test'))))
    doc=SimpleNamespace(file_id='file',file_name='discharge.pdf',file_size=18,mime_type='application/pdf')
    await bot.media(update(document=doc),ctx)
    with storage.SessionLocal() as db:
        saved=db.scalar(select(OperationAttachment).where(OperationAttachment.operation_id==oid));assert saved.data==b'%PDF-1.4 test'
    assert ctx.user_data['operation_flow']['step']=='detail'
    await say('⬅️ Главное меню');assert 'operation_flow' not in ctx.user_data
