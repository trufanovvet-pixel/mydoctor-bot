"""Clinical safety/retrieval scenarios, plus actual web/Telegram wiring.

LLM calls are mocked: these tests verify supplied context and control flow, not
clinical correctness of generated model answers or live production behaviour.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
import knowledge
import exotic_medicine as ex
import app

CASES = json.loads(Path(__file__).with_name('exotic_scenarios.json').read_text())

@pytest.mark.parametrize('case,text,species,urgency,protocol', CASES, ids=[c[0] for c in CASES])
def test_clinical_scenarios(case,text,species,urgency,protocol):
    context, detected, decision = knowledge.build_clinical_context(text)
    assert detected == {species}
    assert decision.urgency == urgency
    assert protocol in {p['id'] for p in knowledge.match_protocols(text)}
    assert 'ВИДОСПЕЦИФИЧНАЯ БЕЗОПАСНОСТЬ' in context
    assert not any(not p.get('id','').startswith('exotic_') for p in knowledge.match_protocols(text))

@pytest.mark.parametrize('text,needle', [
    ('Кролик. Можно Фронтлайн?', 'Фипронил противопоказан'),
    ('Кролику амоксициллин в рот', 'пероральном'),
    ('Кролику Синулокс', 'пероральном'),
    ('Морская свинка, амоксициллин в уколах?', 'Смена пути'),
    ('Морская свинка, клиндамицин?', 'энтеротоксемии'),
    ('Черепахе Ивермек?', 'Ивермектин противопоказан'),
    ('Tortoise ivermectin dose?', 'Ивермектин противопоказан'),
    ('Черепахе мильбемицин?', 'мильбемицина'),
    ('Хомяку линкомицин?', 'энтеротоксемию'),
])
def test_drug_restrictions(text,needle):
    _,_,safety=knowledge.build_clinical_context(text)
    assert needle in '\n'.join(safety.restrictions)

@pytest.mark.parametrize('text', [
    'Крысе амоксициллин по назначению врача',
    'Собаке Синулокс', 'Коту Фронтлайн',
    'У собаки крысиный яд', 'Кошка проглотила хомячий корм',
])
def test_no_false_species_contraindications(text):
    _, species, safety=knowledge.build_clinical_context(text)
    assert not safety.restrictions
    assert 'hamster' not in species

@pytest.mark.parametrize('text', [
    'Попугай не задыхается, ест нормально',
    'Хорек, судорог нет, активен',
    'Хорек без судорог, не шатается',
    'My ferret has no seizures and is eating',
])
def test_negated_signs_do_not_force_emergency(text):
    assert knowledge.build_clinical_context(text)[2].urgency=='unclassified'


def test_dog_cat_protocols_and_diagnostic_ranges_are_not_given_to_exotics():
    for name in ['кролик','попугай','морская свинка','черепаха','хорек','шиншилла','ежик']:
        protocols=knowledge.match_protocols(name+' рвота кровь понос')
        assert all(p.get('id','').startswith('exotic_') for p in protocols)
        assert 'видовые референсы' in knowledge.diagnostics_context(ex.detect_species(name))
    assert any(not p.get('id','').startswith('exotic_') for p in knowledge.match_protocols('Собака рвота'))


def test_followup_keeps_species_and_drug_but_not_old_emergency():
    context,species,safety=knowledge.build_clinical_context('Сейчас ест, вздутия нет, какой контроль?', 'Кролик не ест, живот вздут. Дал амоксициллин внутрь')
    assert species=={'rabbit'} and safety.urgency=='unclassified'
    assert 'пероральном' in safety.prompt()
    assert 'Кролики:' in context


def test_current_patient_overrides_old_case_and_does_not_inherit_drugs():
    context,species,safety=knowledge.build_clinical_context('Теперь крыса чихает', 'Кролик не ест, дал амоксициллин')
    assert species=={'rat'} and not safety.restrictions
    assert 'Кролики: антибиотики' not in context


def test_explicit_selected_species_and_breed_support_short_question():
    context,species,safety=knowledge.build_clinical_context('Сколько дать ивермектина?', pet_species='Рептилия красноухая черепаха')
    assert species=={'chelonian'} and safety.restrictions
    assert 'Ивермектин противопоказан' in context


def test_telegram_emergency_bypasses_interview_and_diagnostics(raw):
    wrapper=app._ResponsesWithKnowledge(raw.responses)
    wrapper.create(instructions='', input=[{'role':'user','content':'Кролик не ест, живот вздут. Какие анализы сдать?'}])
    assert len(raw.responses.calls)==1
    instructions=raw.responses.calls[-1]['instructions']
    assert 'ТЕКУЩАЯ СТАДИЯ: EMERGENCY' in instructions
    assert 'не докармливать' in instructions
    assert 'СРАВНЕНИЕ АНАЛИЗОВ И ОБСЛЕДОВАНИЙ' not in instructions


def test_telegram_short_followup_uses_profile_without_system_species_pollution(raw):
    app._ResponsesWithKnowledge(raw.responses).create(instructions='Правила для собак и кошек\n\nДанные активного питомца:\nИмя: Кеша; Вид: Птица; Порода: корелла',input=[{'role':'user','content':'Не ест'}])
    assert 'Птицы:' in raw.responses.calls[-1]['instructions']
    assert 'ТЕКУЩАЯ СТАДИЯ: ASSESSMENT' in raw.responses.calls[-1]['instructions']


def test_reference_question_still_has_drug_safety(raw):
    import enhanced_app2
    client=app._ClientWithKnowledge(raw)
    enhanced_app2._ReferenceAwareResponses(client).create(instructions='',input=[{'role':'user','content':'Расскажи про ивермектин у черепах'}])
    assert 'Ивермектин противопоказан' in raw.responses.calls[-1]['instructions']


def test_web_chat_uses_medical_context_and_pet_edit_preserves_species():
    from test_web_app import reset_db,register,web_app
    from sqlalchemy import select
    import storage
    reset_db();client=web_app.app.test_client();register(client)
    result=client.post('/pets',data={'name':'Кеша','species':'Птица','breed':'корелла','weight':'0.09'})
    assert result.status_code==302
    with client.session_transaction() as s: uid=s['uid']
    with storage.SessionLocal() as db:
        pid=db.scalar(select(storage.Pet.id).where(storage.Pet.user_id==uid))
    html=client.get(f'/pets/{pid}').get_data(as_text=True)
    assert 'value="Птица" selected' in html
    client.post(f'/pets/{pid}',data={'name':'Кеша','species':'Птица','weight':'0.085'})
    with storage.SessionLocal() as db:
        assert db.get(storage.Pet,pid).species=='Птица'
        assert db.get(storage.Pet,pid).weight_kg==0.085
    with patch.object(web_app.client.responses,'create',return_value=SimpleNamespace(output_text='Тестовый ответ')) as call:
        r=client.post('/api/chat',json={'message':'Кеша не ест'})
        assert r.status_code==200
        assert 'ВИДОСПЕЦИФИЧНАЯ' in call.call_args.kwargs['instructions']
        assert 'same_day' in call.call_args.kwargs['instructions']
        assert 'Птицы:' in call.call_args.kwargs['instructions']


def test_new_protocols_have_sources_and_no_unverified_numeric_doses():
    import medical_evidence as evidence
    for p in knowledge.PROTOCOLS:
        if not p.get('id','').startswith('exotic_'):continue
        assert p['sources'] and all(s.get('url','').startswith('https://') for s in p['sources'])
        assert evidence.review_for(p)
        assert not evidence.DOSE_PATTERN.search(json.dumps(p,ensure_ascii=False))


def test_profile_breed_identifies_turtle_not_just_reptile():
    hint=ex.species_hint('Правила для собак\nДанные активного питомца:\nИмя: Торт; Вид: Другая рептилия; Порода: красноухая черепаха')
    assert ex.resolve_species('Можно ивермек?', pet_species=hint)=={'chelonian'}


async def test_telegram_pet_creation_accepts_bird_and_gram_weight(bot,ctx,update):
    import storage
    await bot.message(update('➕ Добавить питомца'),ctx)
    for value in ['Кеша','Птица','корелла','2 года','Самец','0.09']:
        await bot.message(update(value),ctx)
    pets=storage.list_pets(100)
    assert pets[-1]['species']=='птица' and pets[-1]['weight_kg']==0.09


@pytest.mark.parametrize('button', ['💉 Вакцинация','🪱 Глисты','🪲 Блохи и клещи'])
async def test_telegram_prevention_shortcuts_do_not_send_canine_table(bot,ctx,update,raw,button):
    import storage
    storage.ensure_user(100)
    storage.add_pet(100,'Крош','кролик','','1 год','самец',1.1)
    request=update(button)
    await bot.message(request,ctx)
    assert raw.responses.calls
    assert 'Кролики:' in raw.responses.calls[-1]['instructions']
    assert 'Щенок:' not in str(request.message.reply_text.call_args_list)


def test_named_web_patient_followup_keeps_species_until_new_chat():
    from test_web_app import reset_db,register,web_app
    reset_db();client=web_app.app.test_client();register(client)
    client.post('/pets',data={'name':'Торт','species':'Черепаха','weight':'0.3'})
    with patch.object(web_app.client.responses,'create',return_value=SimpleNamespace(output_text='Тестовый ответ')) as call:
        assert client.post('/api/chat',json={'message':'Торт: хочу проверить паразитов'}).status_code==200
        assert client.post('/api/chat',json={'message':'Можно ивермек?'}).status_code==200
        assert 'Ивермектин противопоказан' in call.call_args.kwargs['instructions']
        client.post('/api/chat/clear')
        assert client.post('/api/chat',json={'message':'Что такое ивермектин?'}).status_code==200
        assert 'Вид/группа по данным владельца: chelonian' not in call.call_args.kwargs['instructions']
