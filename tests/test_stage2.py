import json
from pathlib import Path

import pytest
import app
import storage
import knowledge

FIRST = ('Собака, 9 лет, 12 кг. Шесть дней после операции на колене. '
         'Даю мелоксикам и вчера и позавчера дала преднизолон. Рвота, мало ест, '
         'много пьёт и меньше мочи. Креатинин 165 при норме до 140. Давать лекарство?')
SECOND = ('Сейчас кал чёрный липкий, дёсны бледные, пошатнулся и лёг. '
          'Можно дождаться утра? Есть ибупрофен.')


async def test_initial_medications_survive_long_interview_and_restart(bot, ctx, update, raw):
    await bot.message(update(FIRST), ctx)
    for i in range(8):
        await bot.message(update(f'Уточнение {i}: состояние пока прежнее'), ctx)
    ctx.user_data.clear()  # Simulated process restart, hydrate from isolated SQLite.
    await bot.message(update(SECOND), ctx)
    sent = json.dumps(raw.responses.calls[-1]['input'], ensure_ascii=False)
    assert FIRST in sent
    assert SECOND in sent


def test_controller_does_not_treat_previous_ai_claim_as_owner_fact(raw):
    app._ResponsesWithKnowledge(raw.responses).create(instructions='', input=[
        {'role': 'user', 'content': FIRST},
        {'role': 'assistant', 'content': 'Собака 18 кг, чёрный кал. Бюджет ограничен.'},
        {'role': 'user', 'content': 'Что дальше?'}])
    controller = raw.responses.calls[0]
    assert all(m['role'] != 'assistant' for m in controller['input'])


async def test_pet_switch_removes_previous_patient_context(bot, ctx, update, raw):
    storage.ensure_user(100)
    a = storage.add_pet(100, 'Гром', 'собака', '', '9', '', 12)
    b = storage.add_pet(100, 'Барсик', 'кошка', '', '6', '', 18)
    await bot.pet_callback(update(callback=f"pet:{a['id']}"), ctx)
    await bot.message(update(FIRST), ctx)
    await bot.pet_callback(update(callback=f"pet:{b['id']}"), ctx)
    await bot.message(update('У Барсика чешется ухо'), ctx)
    assert 'мелоксикам' not in json.dumps(raw.responses.calls[-1]['input'], ensure_ascii=False)


def test_draft_and_species_visible_to_model():
    p = next(p for p in knowledge.PROTOCOLS if p.get('id') == 'aki_v1')
    text = knowledge._structured_protocol(p)
    assert 'draft_for_clinician_review' in text
    assert 'Виды:' in text


def test_unverified_numeric_dose_is_not_sent_to_model():
    text = knowledge._structured_protocol({'title': 'Synthetic unreviewed protocol',
        'triggers': ['test'], 'treatment': {'regimen': 'Drug 999 мг/кг q12h'}})
    assert '999' not in text
    assert 'не подтверждена' in text


def test_diagnostics_not_falsely_certified():
    assert 'ПРОВЕРЕННАЯ БАЗА' not in knowledge.diagnostics_context()


@pytest.mark.parametrize('text', [FIRST, SECOND])
def test_owner_case_retrieves_bleeding_and_interaction(text):
    assert any('Желудочно-кишечное кровотечение' in p['title'] for p in knowledge.match_protocols(text))


def test_changed_verified_dose_is_quarantined():
    from copy import deepcopy
    p = deepcopy(next(p for p in knowledge.PROTOCOLS if p.get('id') == 'canine_babesiosis_v1'))
    p['etiotropic_treatment']['large_babesia']['regimen'] = 'Имидокарб 999 мг/кг'
    text = knowledge._structured_protocol(p)
    assert '999' not in text
    assert '13.3' in text  # Other verified regimen remains usable.


def test_every_evidence_receipt_matches_current_field():
    import medical_evidence as evidence
    assert len(evidence.REGISTRY['protocols']) == len(knowledge.PROTOCOLS)
    for p in knowledge.PROTOCOLS:
        review = evidence.review_for(p)
        for path in review['claims']:
            value = p
            for part in path.strip('/').split('/'):
                value = value[int(part)] if isinstance(value, list) else value[part]
            assert evidence.verified_claim(review, path, value), (p['title'], path)


async def test_medical_memory_marks_model_interpretation(bot, ctx, update, pet, raw):
    import medical_memory_patch as memory
    import pet_records_patch as records
    for i in range(2):
        doc_id = records._save_document(100,pet['id'],f'file{i}',f'unique{i}','document',
            'application/pdf',f'lab{i}.pdf','анализ','analysis',None)
        memory._save_insight(doc_id,'Гемоглобин 130 — интерпретация ИИ')
    ctx.user_data['dialog_scope'] = 'pet'
    await bot.message(update('Сравни анализы Грома'),ctx)
    sent = json.dumps(raw.responses.calls[-1]['input'],ensure_ascii=False)
    assert 'не оригиналы бланков' in sent
    assert 'Сохранённая интерпретация ИИ' in sent


def test_interaction_with_adverse_signs_cannot_be_question_only(raw):
    wrapped = app._ResponsesWithKnowledge(raw.responses)
    decision = wrapped._controller_decision({'input':[{'role':'user','content':FIRST}]},'')
    assert decision['stage'] == 'ASSESSMENT'
    benign = wrapped._controller_decision({'input':[{'role':'user','content':'Что такое преднизолон?'}]},'')
    assert benign['stage'] == 'INTERVIEW'
