import json
from pathlib import Path

import pytest
import knowledge

CASES = json.loads(Path(__file__).with_name('scenarios.json').read_text())


@pytest.mark.parametrize('case,text,filename', CASES, ids=[c[0] for c in CASES])
def test_owner_language(case, text, filename):
    expected = json.loads((knowledge.PROTOCOL_DIR / (filename + '.json')).read_text())
    assert expected in knowledge.match_protocols(text), text


@pytest.mark.parametrize('path', sorted(knowledge.PROTOCOL_DIR.glob('*.json')), ids=lambda p:p.stem)
def test_every_protocol_loaded_and_reachable(path):
    data = json.loads(path.read_text())
    assert data in knowledge.PROTOCOLS
    assert any(data in knowledge.match_protocols(t) for t in data['triggers'] + [data['title']])


@pytest.mark.parametrize('path', sorted(knowledge.PROTOCOL_DIR.glob('*.json')), ids=lambda p:p.stem)
def test_no_clinical_fields_lost(path):
    data = json.loads(path.read_text())
    rendered = knowledge._structured_protocol(data)
    def leaves(value):
        if isinstance(value, dict):
            for child in value.values(): yield from leaves(child)
        elif isinstance(value, list):
            for child in value: yield from leaves(child)
        elif isinstance(value, str): yield value
    metadata = {'id','status','version','reviewed_at','species','triggers','audiences'}
    for key, value in data.items():
        if key not in metadata:
            assert all(leaf in rendered for leaf in leaves(value)), (path.name, key)


@pytest.mark.parametrize('text,unwanted', [
    ('Собака съела шоколад', 'Шок и первичная стабилизация'),
    ('У собаки дисплазия', 'Гематология, коагулопатии и тромбоз'),
    ('Покажи календарь', 'Тромбоз и тромбоэмболия'),
])
def test_no_substring_false_positives(text, unwanted):
    assert unwanted not in [p['title'] for p in knowledge.match_protocols(text)]


@pytest.mark.parametrize('text', ['Кот не может помочиться', 'У собаки кровь в кале', 'У кошки моча по каплям'])
def test_symptoms_are_not_diagnostic_comparisons(text):
    assert not knowledge.is_diagnostics_query(text)
