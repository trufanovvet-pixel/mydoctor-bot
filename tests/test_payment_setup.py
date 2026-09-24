import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select, func
import billing as b
import storage
from payment_setup import apply_configuration
from test_web_app import reset_db


@pytest.fixture(autouse=True)
def clean():
    reset_db()


def configuration():
    return {'revision': 'test-v1', 'open_sales': True, 'methods': [
        {'transfer': 'mir', 'currency': 'RUB', 'instructions': 'TEST ONLY bank and card'},
        {'transfer': 'sbp', 'currency': 'RUB', 'instructions': 'TEST ONLY phone, bank, recipient'},
    ]}


def test_configuration_is_atomic_idempotent_and_preserves_later_doctor_edits(monkeypatch):
    monkeypatch.setenv('MYDOCTOR_PAYMENT_SETUP', json.dumps(configuration()))
    with ThreadPoolExecutor(max_workers=3) as pool:
        applied = list(pool.map(lambda _: apply_configuration(), range(3)))
    assert applied.count(True) == 1 and b.live()
    with storage.SessionLocal() as db:
        assert set(b.card_methods(db)) == {'mir', 'sbp'}
        assert db.scalar(select(func.count(b.PaymentMethod.id))) == 2
    mid = b.save_card_method('mir', 'RUB', 'TEST ONLY doctor updated receiving details', {})
    storage.set_bot_setting('billing_live', '0')
    assert not apply_configuration() and not b.live()
    with storage.SessionLocal() as db: assert b.card_methods(db)['mir']['method'].id == mid


def test_invalid_second_method_cannot_partially_enable_payments(monkeypatch):
    config = configuration(); config['methods'][1]['currency'] = 'USD'
    monkeypatch.setenv('MYDOCTOR_PAYMENT_SETUP', json.dumps(config))
    with pytest.raises(b.BillingError): apply_configuration()
    assert not b.live()
    with storage.SessionLocal() as db: assert not db.scalar(select(b.PaymentMethod.id))
