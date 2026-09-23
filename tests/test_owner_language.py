import re
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import pytest
from sqlalchemy import select
import storage
with patch('openai.OpenAI'):
    import web_app
from owner_profile import OwnerProfile, ConsultationContact
from consultation_delivery import deliver_pending
from test_web_app import reset_db, register


def token(client,path,name):
    html=client.get(path).get_data(as_text=True)
    return re.search('name="'+name+'" value="([^"]+)"',html).group(1)


def contacts(client,**changes):
    return dict(name='Alex Owner',phone='+66 81 234 5678',email='alex@example.com',telegram='https://t.me/alex_owner',
                whatsapp='+66 82 234 5678',instagram='https://www.instagram.com/alex.owner/',vk='https://vk.ru/alex_owner',
                preferred='whatsapp',profile_key=token(client,'/profile','profile_key'),**changes)


def test_profile_is_private_validated_and_prefills_consultation():
    reset_db();a=web_app.app.test_client();b=web_app.app.test_client()
    register(a,'a@example.com');register(b,'b@example.com')
    values=contacts(a)
    assert a.post('/profile',data=values).status_code==303
    with a.session_transaction() as s:uid=s['uid']
    with storage.SessionLocal() as db:
        row=db.get(OwnerProfile,uid)
        assert row.telegram=='alex_owner' and row.instagram=='alex.owner' and row.vk=='alex_owner'
    html=a.get('/consultation').get_data(as_text=True)
    assert 'value="+66 82 234 5678"' in html and 'alex@example.com' in html
    assert 'alex@example.com' not in b.get('/profile').get_data(as_text=True)
    assert 'alex@example.com' not in b.get('/consultation').get_data(as_text=True)
    for field,value in [('telegram','https://evil.example/name'),('instagram','a/b'),('vk','javascript:evil'),('email','invalid'),('whatsapp','abc')]:
        assert a.post('/profile',data={**values,field:value}).status_code==400
    assert a.post('/profile',data={**values,'profile_key':'bad'}).status_code==400
    with storage.SessionLocal() as db:assert db.get(OwnerProfile,uid).email=='alex@example.com'


@pytest.mark.asyncio
async def test_request_contacts_are_a_snapshot_with_messenger_buttons():
    reset_db();c=web_app.app.test_client();register(c)
    saved=contacts(c);c.post('/profile',data=saved)
    key=token(c,'/consultation','request_key')
    result=c.post('/consultation',data={'contact':'+66 82 234 5678','question':'Follow-up','pet_summary':'Dog, 5 years, 12 kg','format':'Переписка','request_key':key})
    assert result.status_code==303
    # Changing the profile later must not change the recipient links on an existing request.
    c.post('/profile',data={**saved,'telegram':'other_owner'})
    app=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(),send_document=AsyncMock()))
    await deliver_pending(app)
    payload=app.bot.send_message.call_args.kwargs
    assert 'alex@example.com' in payload['text'] and 'Alex Owner' in payload['text']
    buttons=payload['reply_markup'].inline_keyboard
    links={row[0].text:row[0].url for row in buttons if row[0].text in ('Telegram','WhatsApp','Instagram','VK')}
    assert links=={'Telegram':'https://t.me/alex_owner','WhatsApp':'https://wa.me/66822345678','Instagram':'https://www.instagram.com/alex.owner/','VK':'https://vk.com/alex_owner'}


class VisibleText(HTMLParser):
    def __init__(self):super().__init__();self.hidden=0;self.text=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.hidden-=1
    def handle_data(self,data):
        if not self.hidden:self.text.append(data)


def test_english_pages_have_translated_interface_and_stable_form_values():
    reset_db();c=web_app.app.test_client();c.get('/language/en');register(c)
    for path in ['/','/login','/register','/dashboard','/how-it-works','/profile','/pets','/analyses','/operations','/assistant','/prevention','/consultation','/history']:
        response=c.get(path);assert response.status_code==200,path
        html=response.get_data(as_text=True);assert 'lang="en"' in html
        visible=VisibleText();visible.feed(html)
        assert not re.search('[А-Яа-яЁё]',''.join(visible.text)),(path,''.join(visible.text))
    html=c.get('/pets').get_data(as_text=True)
    assert 'value="Собака">Dog</option>' in html
    c.post('/pets',data={'name':'Archie','species':'Собака','weight':'12'})
    with c.session_transaction() as s:uid=s['uid']
    with storage.SessionLocal() as db:pid=db.scalar(select(storage.Pet.id).where(storage.Pet.user_id==uid))
    assert 'Dog' in c.get(f'/pets/{pid}').get_data(as_text=True)


def test_language_persists_and_cannot_redirect_off_site():
    reset_db();c=web_app.app.test_client();register(c);c.post('/profile',data=contacts(c))
    assert c.get('/language/en?next=/profile').headers['Location']=='/profile'
    for target in ['//evil.example','https://evil.example','/\\evil.example']:
        assert c.get('/language/en',query_string={'next':target}).headers['Location']=='/'
    c.delete_cookie('language')
    assert 'lang="en"' in c.get('/profile').get_data(as_text=True)
    c.get('/language/ru')
    assert 'lang="ru"' in c.get('/profile').get_data(as_text=True)


def test_assistant_uses_english_and_request_status_is_localized():
    reset_db();c=web_app.app.test_client();register(c);c.get('/language/en')
    with patch.object(web_app.client.responses,'create',return_value=SimpleNamespace(output_text='How can I help?')) as call:
        assert c.post('/api/chat',json={'message':'What is an MRI?'}).json['answer']=='How can I help?'
        assert 'Reply in English' in call.call_args.kwargs['instructions']
    key=token(c,'/consultation','request_key')
    location=c.post('/consultation',data={'contact':'alex@example.com','question':'Follow-up','pet_summary':'Dog, 5 years, 12 kg','format':'Переписка','request_key':key}).headers['Location']
    assert 'Your request is saved' in c.get(location+'?status=1').json['message']
    assert 'Preferred language: English' in c.get(location).get_data(as_text=True)
