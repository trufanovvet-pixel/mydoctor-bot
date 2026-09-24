import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Never use production credentials or production storage in the regression suite.
_TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix='mydoctor-test-')
os.environ['DATABASE_URL'] = 'sqlite:///' + _TEST_DIRECTORY.name + '/tests.db'
os.environ.pop('OPENAI_API_KEY', None)
os.environ.pop('TELEGRAM_BOT_TOKEN', None)
os.environ.pop('MYDOCTOR_PAYMENT_SETUP', None)

import storage
import launcher
import app
import enhanced_app2


class Responses:
    def __init__(self): self.calls = []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if 'скрытый клинический диспетчер' in kwargs.get('instructions', ''):
            return SimpleNamespace(output_text='{"stage":"INTERVIEW","questions":["Как давно?"]}')
        return SimpleNamespace(output_text='Тестовый ответ ' + str(len(self.calls)))


@pytest.fixture
def raw():
    return SimpleNamespace(responses=Responses(), audio=SimpleNamespace(transcriptions=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(text='Гром'))))


@pytest.fixture
def bot(raw):
    storage.Base.metadata.create_all(storage.engine)
    with storage.engine.begin() as connection:
        for table in reversed(storage.Base.metadata.sorted_tables):
            connection.execute(table.delete())
    b = app._load_bot_module()
    # Exercise the same construction and patch order as production.
    from unittest.mock import patch
    b.client = raw
    with patch.object(app, '_load_bot_module', return_value=b):
        return enhanced_app2.build_bot()


@pytest.fixture
def ctx():
    return SimpleNamespace(user_data={}, bot=SimpleNamespace(
        get_file=AsyncMock(), send_message=AsyncMock(), send_photo=AsyncMock(), send_document=AsyncMock()))


def make_update(text='', user_id=100, callback=None, photo=None, document=None, caption=None, voice=None):
    msg = SimpleNamespace(text=text, photo=photo or [], document=document, caption=caption, voice=voice,
        reply_text=AsyncMock(), chat=SimpleNamespace(send_action=AsyncMock()))
    query = SimpleNamespace(data=callback, answer=AsyncMock(), edit_message_text=AsyncMock(), message=msg) if callback else None
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=query,
        effective_user=SimpleNamespace(id=user_id, username='qa_test', first_name='QA'),
        effective_chat=SimpleNamespace(id=user_id, type='private'))


@pytest.fixture
def update(): return make_update


@pytest.fixture
def pet(bot):
    storage.ensure_user(100)
    return storage.add_pet(100,'Гром','собака','метис','5 лет','самец',10.0)
