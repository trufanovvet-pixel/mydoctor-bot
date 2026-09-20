import asyncio
import io
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from PIL import Image

import app
import enhanced_app2
import storage
import conversation_store
import knowledge
import media_direct_client_patch
import pet_records_patch as records
import medical_memory_patch as memory
import prevention_patch as prevention
import prevention_time_patch as timing
import prevention_smart_patch as smart
import web_search_patch as web


async def test_add_pet_from_inline_button(bot,ctx,update):
    await bot.add_pet_callback(update(callback='pets:add'),ctx)
    for text in ['Гром','🐶 Собака','метис','5 лет','Самец','8,8']:
        await bot.message(update(text),ctx)
    assert len(storage.list_pets(100)) == 1
    assert storage.get_active_pet(100)['weight_kg'] == 8.8


async def test_add_pet_from_legacy_button(bot,ctx,update):
    for text in ['➕ Добавить питомца','Гром','🐶 Собака','метис','5 лет','Самец','8,8']:
        await bot.message(update(text),ctx)
    assert storage.get_active_pet(100)['name'] == 'Гром'
    assert 'adding_pet' not in ctx.user_data


@pytest.mark.parametrize('weight', ['0','-1','nan','inf','-inf','1000000'])
async def test_invalid_weight_rejected(bot,ctx,update,weight):
    for text in ['➕ Добавить питомца','Гром','🐶 Собака','метис','5 лет','Самец',weight]:
        await bot.message(update(text),ctx)
    assert storage.list_pets(100) == []


async def test_history_keeps_assistant_after_twelve_turns(bot,ctx,update):
    for i in range(14): await bot.message(update(f'Сегодня симптом номер {i}'),ctx)
    restored = conversation_store.load_conversation_history(100,30)
    assert len(restored) == 28
    assert [m['role'] for m in restored] == ['user','assistant']*14


async def test_reference_receives_full_specialist_knowledge(bot,ctx,update,raw):
    await bot.message(update('Что такое бронхоскопия?'),ctx)
    instructions=raw.responses.calls[-1]['instructions']
    assert 'Эндоскопия' in instructions and 'procedures:' in instructions


@pytest.mark.parametrize('output',['[]','null','42','"text"','{bad json'])
def test_controller_malformed_json_does_not_crash(output):
    assert app._parse_controller_output(output)['stage'] == 'INTERVIEW'


def test_diagnostics_marker_does_not_stick(raw):
    wrapper=app._ResponsesWithKnowledge(raw.responses)
    wrapper.create(input=[{'role':'user','content':'[DIAGNOSTICS_MODE]'},
        {'role':'user','content':'ПЦР или ИФА?'},{'role':'assistant','content':'Ответ'},
        {'role':'user','content':'Собака не дышит'}],instructions='')
    assert 'РЕЖИМ «АНАЛИЗЫ И ОБСЛЕДОВАНИЯ»' not in raw.responses.calls[-1]['instructions']


def test_old_assistant_diagnosis_does_not_override_new_question(raw):
    wrapper=app._ResponsesWithKnowledge(raw.responses)
    wrapper.create(input=[{'role':'assistant','content':'рвота '*50},
        {'role':'user','content':'Кот не может помочиться'}],instructions='')
    assert 'Уретральная обструкция' in raw.responses.calls[-1]['instructions']


def test_media_wrapper_keeps_parts_and_medical_knowledge(raw):
    wrapper=media_direct_client_patch._MediaSafeResponses(raw,raw)
    media=[{'role':'user','content':[{'type':'input_image','image_url':'data:image/jpeg;base64,eA=='},
        {'type':'input_text','text':'Анализ крови, объясни низкие тромбоциты'}]}]
    wrapper.create(input=media,instructions='rules')
    assert raw.responses.calls[-1]['input'] == media
    assert 'БАЗА' in raw.responses.calls[-1]['instructions']


def test_web_search_accepts_multimodal_history():
    assert web._trim_input([{'role':'user','content':[{'type':'input_text','text':'привет'}]}])


def test_old_clinic_search_does_not_capture_next_medical_question():
    messages=[{'role':'user','content':'Найди клинику в Туле'},
        {'role':'assistant','content':'Клиники'}, {'role':'user','content':'У собаки рвота'}]
    assert not web._needs_local_search(messages)


@pytest.mark.parametrize('text',['Найди клинику в Туле','Посоветуй хорошего ветеринара в Щёкино','Где ближайшая ветклиника?'])
def test_clinic_search_still_routes(text):
    assert web._needs_local_search([{'role':'user','content':text}])


@pytest.mark.parametrize('text',['⬅️ Главное меню','❌ Отмена','/menu'])
async def test_cancel_prevention_date(bot,ctx,update,pet,text):
    await bot.message(update('➕ Вакцинация'),ctx)
    assert ctx.user_data.get('prevention_flow')
    if text=='/menu': await bot.menu_command(update(text),ctx)
    else: await bot.message(update(text),ctx)
    assert not ctx.user_data.get('prevention_flow')


def test_explicit_date_embedded_in_message():
    assert smart._parse_relative_date('Напомни о вакцинации 25.10.2030').isoformat()=='2030-10-25'


def test_silly_date_does_not_crash():
    assert smart._parse_relative_date('Напомни через 999999999999 лет о вакцинации') is None


def test_pet_name_inflection_resolves(pet):
    other={'id':999,'name':'Барсик'}
    assert records._pet_from_text([pet,other],'Покажи анализы Грома')['id']==pet['id']
    assert memory._pet_from_text([pet,other],'Сравни анализы Грома')['id']==pet['id']


def test_archive_request_does_not_capture_medical_question():
    assert not records._looks_like_archive_request('Какой анализ покажет инфекцию?')
    assert not records._looks_like_archive_request('Назначили анализы, подскажи что делать')


def test_user_pet_isolation(bot,pet):
    storage.ensure_user(200)
    assert storage.set_active_pet(200,pet['id']) is None
    assert records._documents_for_pet(200,pet['id']) == []
    assert not prevention._save_event(200,pet['id'],'vaccination',smart._today())


async def test_failed_media_does_not_reuse_old_analysis(bot,ctx,update,pet):
    ctx.user_data.update(dialog_scope='pet',history=[{'role':'assistant','content':'Старый анализ крови 01.01.2020'}])
    doc=SimpleNamespace(file_id='bad',file_unique_id='bad',mime_type='image/jpeg',file_name='bad.jpg',file_size=100)
    ctx.bot.get_file.side_effect=RuntimeError('download failed')
    await bot.media(update(document=doc),ctx)
    with storage.SessionLocal() as session:
        assert session.scalar(select(memory.PetDocumentInsight)) is None
        assert session.scalar(select(records.PetDocument)) is None


async def test_general_question_not_saved_to_active_pet(bot,ctx,update,pet):
    ctx.user_data['dialog_scope']='general'
    await bot.message(update('Что такое гастроскопия?'),ctx)
    with storage.SessionLocal() as session:
        assert session.scalar(select(storage.Consultation)).pet_id is None


async def test_switch_pet_clears_document_target(bot,ctx,update,pet):
    other=storage.add_pet(100,'Барсик','кошка',None,None,None,4)
    ctx.user_data.update(records_target_explicit=True,records_target_pet_id=pet['id'])
    await bot.pet_callback(update(callback=f"pet:{other['id']}"),ctx)
    assert ctx.user_data.get('records_target_pet_id') != pet['id']


def test_reminder_not_marked_before_send(bot,pet,monkeypatch):
    now=datetime(2030,10,25,11)
    monkeypatch.setattr(timing,'datetime',SimpleNamespace(now=lambda tz:now))
    prevention._save_event(100,pet['id'],'vaccination',now.date())
    assert timing._due_notifications_at_ten()
    assert timing._due_notifications_at_ten(), 'Failed sends must remain eligible for retry'


@pytest.mark.parametrize('kind',['photo','image_document','pdf'])
async def test_successful_media_to_archive_and_memory(bot,ctx,update,pet,kind):
    ctx.user_data['dialog_scope']='pet'
    payload=io.BytesIO()
    Image.new('RGB',(80,120),'white').save(payload,format='JPEG')
    content=payload.getvalue() if kind!='pdf' else b'%PDF-1.4\n%%EOF'
    ctx.bot.get_file.return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=content))
    item=SimpleNamespace(file_id=kind,file_unique_id=kind,file_size=len(content),
        mime_type='application/pdf' if kind=='pdf' else 'image/jpeg',file_name='analysis.pdf' if kind=='pdf' else 'analysis.jpg')
    message=update(photo=[item] if kind=='photo' else [],document=item if kind!='photo' else None,
        caption='Анализ крови от 20.09.2026')
    await bot.media(message,ctx)
    with storage.SessionLocal() as session:
        doc=session.scalar(select(records.PetDocument))
        insight=session.scalar(select(memory.PetDocumentInsight))
        assert doc.pet_id==pet['id'] and doc.document_date==datetime(2026,9,20)
        assert insight.document_id==doc.id and 'Тестовый ответ' in insight.analysis_text
    assert conversation_store.load_conversation_history(100)[-1]['role']=='assistant'


async def test_voice_uses_pet_form_router(bot,ctx,update):
    await bot.add_pet_callback(update(callback='pets:add'),ctx)
    ctx.bot.get_file.return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=b'audio'))
    await bot.voice_message(update(voice=SimpleNamespace(file_id='voice',file_size=5)),ctx)
    assert ctx.user_data['adding_pet']['name']=='Гром'
    assert ctx.user_data['adding_pet']['step']=='species'


async def test_voice_uses_archive_router(bot,ctx,update,pet,raw):
    raw.audio.transcriptions.create=lambda **kw: SimpleNamespace(text='Покажи анализы Грома')
    ctx.bot.get_file.return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=b'audio'))
    request=update(voice=SimpleNamespace(file_id='voice',file_size=5))
    await bot.voice_message(request,ctx)
    assert 'сохранённых файлов' in request.message.reply_text.call_args.args[0]


async def test_reminders_retry_failed_delivery(bot,ctx,pet,monkeypatch):
    now=datetime(2030,10,25,11)
    monkeypatch.setattr(timing,'datetime',SimpleNamespace(now=lambda tz:now))
    prevention._save_event(100,pet['id'],'vaccination',now.date())
    ctx.bot.send_message.side_effect=RuntimeError('offline')
    await prevention._send_due_notifications(ctx)
    assert timing._due_notifications_at_ten()
    ctx.bot.send_message.side_effect=None
    await prevention._send_due_notifications(ctx)
    assert timing._due_notifications_at_ten()==[]


async def test_stale_foreign_pet_callback_does_not_switch(bot,ctx,update,pet):
    storage.ensure_user(200)
    other=storage.add_pet(200,'Чужой','кошка',None,None,None,4)
    await bot.pet_callback(update(callback=f"pet:{other['id']}"),ctx)
    assert storage.get_active_pet(100)['id']==pet['id']
    assert 'dialog_scope' not in ctx.user_data


async def test_consultation_form_keeps_voice_chat_formats(bot,ctx,update):
    await bot.consult_callback(update(callback='consult:start'),ctx)
    await bot.consult_callback(update(callback='consult:type:primary'),ctx)
    for message in ['Владелец','Пёс 5 лет 10 кг','Хромает два дня','нет']:
        await bot.message(update(message),ctx)
    assert ctx.user_data['consult_flow']['step']=='format'
    await bot.consult_callback(update(callback='consult:format:chat'),ctx)
    assert not ctx.user_data.get('consult_flow')
    assert ctx.bot.send_message.called
    with storage.SessionLocal() as session:
        assert session.scalar(select(storage.Consultation)).kind=='consult_request'


async def test_general_scope_survives_restart(bot,ctx,update,pet,raw):
    await bot.message(update('💬 Задать вопрос'),ctx)
    await bot.message(update('🌐 Общий вопрос'),ctx)
    await bot.message(update('Что такое гастроскопия?'),ctx)
    ctx.user_data.clear()
    await bot.message(update('А как проходит подготовка?'),ctx)
    assert ctx.user_data['dialog_scope']=='general'
    assert 'Имя: Гром' not in raw.responses.calls[-1]['instructions']


def test_clinic_location_followup():
    assert web._needs_local_search([{'role':'user','content':'Найди клинику рядом'},
        {'role':'assistant','content':'Какой город?'},{'role':'user','content':'Я в Туле'}])


async def test_unbound_archive_retrieval(bot,ctx,update):
    storage.ensure_user(100)
    records._save_document(100,None,'file','unique','document','application/pdf','lab.pdf','анализ','analysis',None)
    await bot.message(update('Покажи документы без привязки'),ctx)
    assert ctx.bot.send_document.call_args.kwargs['document']=='file'
