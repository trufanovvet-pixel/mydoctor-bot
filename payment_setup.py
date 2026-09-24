"""One-time, atomic receiving-details configuration from protected deployment settings.

No bank details belong in source control. Later doctor-panel edits take precedence
over an already-applied revision, including the decision to pause sales.
"""
import json
import os
import re
from sqlalchemy import text, update
import billing as b
import storage

REVISION_KEY = 'payment_setup_revision'


def apply_configuration():
    raw = os.getenv('MYDOCTOR_PAYMENT_SETUP')
    if not raw:
        return False
    config = json.loads(raw)
    revision = config.get('revision', '')
    methods = config.get('methods', [])
    if not isinstance(revision, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', revision):
        raise ValueError('Invalid payment setup revision')
    if not isinstance(methods, list) or not 1 <= len(methods) <= len(b.CARD_TRANSFERS):
        raise ValueError('Invalid payment setup methods')
    if type(config.get('open_sales')) is not bool:
        raise ValueError('Payment setup must specify open_sales')
    validated, keys = [], set()
    for method in methods:
        key = method['transfer']
        if key in keys:
            raise ValueError('Duplicate payment setup method')
        keys.add(key)
        route, prices = b.validate_card_method(key, method['currency'], method['instructions'], method.get('prices', {}))
        validated.append((route, method['currency'], method['instructions'], prices))
    with storage.SessionLocal() as db:
        if db.bind.dialect.name == 'postgresql':
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': 728190424})
        else:
            # Start a write transaction before checking the marker on SQLite too.
            db.execute(update(storage.BotSetting).where(storage.BotSetting.key == REVISION_KEY).values(key=REVISION_KEY))
        previous = db.get(storage.BotSetting, REVISION_KEY)
        if previous and previous.value == revision:
            return False
        for args in validated:
            b.write_card_method(db, *args)
        db.merge(storage.BotSetting(key=REVISION_KEY, value=revision))
        db.merge(storage.BotSetting(key='billing_live', value='1' if config['open_sales'] else '0'))
        db.commit()
    print('payment setup applied: revision=' + revision + ' methods=' + ','.join(sorted(keys))
          + ' sales=' + ('enabled' if config['open_sales'] else 'disabled'), flush=True)
    return True
