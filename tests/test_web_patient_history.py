import json
from datetime import date, datetime, timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import storage
from care_models import HealthEntry
from patient_records import OperationRecord
from web_patient_history import saved_history


@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')
    storage.Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([storage.User(id=1, telegram_id=1), storage.User(id=2, telegram_id=2)])
        session.add_all([storage.Pet(id=1, user_id=1, name='Лада', species='dog'),
                         storage.Pet(id=2, user_id=1, name='Рекс', species='dog'),
                         storage.Pet(id=3, user_id=2, name='Другой', species='dog')])
        session.flush()
        yield session
    engine.dispose()


def add_entry(db, pet_id, operation_id, notes='наблюдение', when=None):
    e = HealthEntry(pet_id=pet_id, created_by=1, request_key=str(pet_id)+notes,
        operation_id=operation_id, kind='postop', observed_at=when or datetime(2026,11,1,1),
        timezone='Asia/Bangkok', appetite='reduced', activity='reduced', pain='some',
        wound='worse', notes=notes)
    db.add(e)
    db.flush()
    return e


def unpack(value):
    return json.loads(value.split('\n', 2)[2])


def test_dates_and_actual_observation_are_transmitted(db):
    db.add(OperationRecord(id=1,user_id=1,pet_id=1,title='ОГЭ: пиометра',
        operation_date=date(2026,10,29), notes='До хирурга 4 часа'))
    add_entry(db,1,1,'Аппетит снизился, мутное отделяемое')
    data=unpack(saved_history(db,1,1,now=datetime(2026,11,1,0,tzinfo=timezone.utc)))
    assert data['operations'][0]['postoperative_day_today']==3
    assert data['observations'][0]['postoperative_day_at_observation']==3
    assert data['observations'][0]['appetite']=='Снижен'
    assert data['observations'][0]['vomiting'] is None
    assert 'мутное отделяемое' in data['observations'][0]['notes']
    assert data['operations'][0]['notes']=='До хирурга 4 часа'


def test_no_cross_pet_or_owner_records_even_with_invalid_operation_link(db):
    db.add_all([OperationRecord(id=2,user_id=1,pet_id=2,title='РЕКС СЕКРЕТ',operation_date=date(2026,10,25)),
                OperationRecord(id=3,user_id=2,pet_id=3,title='ЧУЖОЙ СЕКРЕТ')])
    add_entry(db,1,3)
    add_entry(db,2,2,'РЕКС ЗАПИСЬ')
    result=saved_history(db,1,1)
    assert 'СЕКРЕТ' not in result and 'РЕКС ЗАПИСЬ' not in result
    assert unpack(result)['observations'][0]['operation_id'] is None
    assert saved_history(db,1,3)==''


def test_local_day_stale_entries_future_and_unknown_operations(db):
    db.add_all([OperationRecord(id=1,user_id=1,pet_id=1,title='Операция',operation_date=date(2026,10,25)),
                OperationRecord(id=2,user_id=1,pet_id=1,title='План',operation_date=date(2026,11,5)),
                OperationRecord(id=3,user_id=1,pet_id=1,title='Без даты')])
    add_entry(db,1,1,when=datetime(2026,10,30,18))
    data=unpack(saved_history(db,1,1,now=datetime(2026,10,31,18,tzinfo=timezone.utc)))
    ops={o['id']:o for o in data['operations']}
    assert data['as_of']=='2026-11-01'
    assert ops[1]['postoperative_day_today']==7
    assert data['observations'][0]['postoperative_day_at_observation']==6
    assert ops[2]['planned'] and ops[2]['postoperative_day_today'] is None
    assert ops[3]['postoperative_day_today'] is None


def test_bounds_and_linked_older_operation(db):
    for i in range(1,8):
        db.add(OperationRecord(id=i,user_id=1,pet_id=1,title=f'Операция {i}',
            created_at=datetime(2026,10,i),operation_date=date(2026,10,i)))
    for i in range(12):
        add_entry(db,1,1,notes=str(i)+'x'*3000,when=datetime(2026,11,1,i))
    data=unpack(saved_history(db,1,1))
    assert len(data['observations'])==10
    assert len(data['observations'][0]['notes'])<2600
    assert 1 in {o['id'] for o in data['operations']}
