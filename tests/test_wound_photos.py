import io
import json
import secrets
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import pytest
from PIL import Image
from sqlalchemy import select, func
import storage
import billing
import wound_photos as wound
from care_models import HealthEntry, WoundPhotoAnalysis, CareMember, CareSharing
from patient_records import OperationRecord
from test_pet_care import account, post, invite
from test_web_app import web_app, reset_db
from test_clinical_safety_access import exhaust


@pytest.fixture(autouse=True)
def clean(): reset_db()


def image(color='blue'):
    out=io.BytesIO(); Image.new('RGB',(40,40),color).save(out,format='JPEG'); return out.getvalue()


def entry(uid,pid,**changes):
    with storage.SessionLocal() as db:
        oid=changes.pop('operation_id',None)
        if oid is None:
            op=OperationRecord(user_id=uid,pet_id=pid,title='Test operation',operation_date=datetime.utcnow().date()-timedelta(days=5))
            db.add(op);db.flush();oid=op.id
        values=dict(created_by=uid,pet_id=pid,operation_id=oid,request_key=secrets.token_hex(16),kind='postop',
            observed_at=datetime.utcnow(),timezone='UTC',appetite='normal',activity='normal',pain='none',
            wound='unchanged',urgency='recorded',photo=image())
        values.update(changes);row=HealthEntry(**values);db.add(row);db.commit();return row.id,oid


def result(**changes):
    value=dict(subject='surgical_wound',quality='adequate',urgency='recorded',trend='not_comparable',
        findings=['Края видимого участка сопоставлены.'],limitations=['Глубину по фото не оценить.'],
        questions=['Были ли выделения?'],concerns=[],comparison='')
    value.update(changes);return value


def response(**changes): return SimpleNamespace(output_text=json.dumps(result(**changes)),model='test-vision')


@pytest.mark.parametrize('reported,concerns,expected',[
    ('urgent',[],'urgent'),('contact',[],'contact'),('recorded',['edge_separation'],'contact'),
    ('recorded',['possible_deep_opening'],'urgent'),('contact',['heavy_bleeding'],'urgent'),
    ('recorded',[],'recorded')])
def test_risk_cannot_be_downgraded(reported,concerns,expected):
    actual=wound.parse_result(json.dumps(result(concerns=concerns)),reported,0)
    assert actual['urgency']==expected


@pytest.mark.parametrize('changes,count',[
    ({},0),({'subject':'other'},2),({'subject':'uncertain'},2),({'quality':'unusable'},2),({'quality':'limited'},2)])
def test_unreadable_or_unrelated_images_cannot_claim_comparison(changes,count):
    actual=wound.parse_result(json.dumps(result(trend='less_concerning',comparison='Улучшение',**changes)),'recorded',count)
    assert actual['trend']=='not_comparable' and actual['comparison']==''
    if changes.get('subject') in ('other','uncertain') or changes.get('quality')=='unusable':
        assert not actual['assessable'] and actual['findings']==[]


@pytest.mark.parametrize('changes',[
    {'quality':'perfect'},{'urgency':'healthy'},{'findings':'normal'},{'concerns':['invented']},
    {'findings':['x'*601]},{'questions':[4]},{'comparison':None}])
def test_malformed_results_rejected(changes):
    with pytest.raises(ValueError): wound.parse_result(json.dumps(result(**changes)),'recorded',0)


def test_history_only_same_pet_operation_and_distinct_earlier_images():
    c,uid,pid=account();now=datetime.utcnow();current,oid=entry(uid,pid,observed_at=now)
    old,_=entry(uid,pid,operation_id=oid,observed_at=now-timedelta(days=2),photo=image('red'))
    recent,_=entry(uid,pid,operation_id=oid,observed_at=now-timedelta(days=1),photo=image('green'))
    entry(uid,pid,operation_id=oid,observed_at=now-timedelta(hours=1)) # same bytes as current
    entry(uid,pid,operation_id=oid,observed_at=now+timedelta(hours=1),photo=image('white'))
    entry(uid,pid,observed_at=now-timedelta(hours=2),photo=image('black')) # another operation
    b,buid,bpid=account('other@example.com')
    entry(buid,bpid,operation_id=oid,observed_at=now-timedelta(hours=1),photo=image('orange'))
    with storage.SessionLocal() as db:
        row=db.get(HealthEntry,current);previous=wound.previous_photos(db,row)
        assert [r.id for r in previous]==[old,recent]
        kwargs=wound.model_request(db.get(storage.Pet,pid),db.get(OperationRecord,oid),row,previous,'en','vision')
    parts=kwargs['input'][0]['content']
    assert len([p for p in parts if p['type']=='input_image'])==3
    assert all(p['image_url'].startswith('data:image/jpeg;base64,') for p in parts if p['type']=='input_image')
    assert 'English' in kwargs['instructions'] and kwargs['text']['format']['strict'] is True
    assert 'days_since_operation' in parts[0]['text']


def test_shared_success_replays_without_new_charge_and_private_html_is_escaped():
    a,uid,pid=account();b,buid,_=account('family@example.com');invite(a,pid,b)
    eid,_=entry(uid,pid);path=f'/care/photos/{eid}/analyse';storage.set_bot_setting('billing_live','1')
    with patch.object(web_app.client.responses,'create',return_value=response(findings=['<script>bad()</script>'])) as model:
        assert post(a,path).status_code==303
        assert post(a,path).status_code==303
        assert post(b,path).status_code==303
        assert model.call_count==1
    with storage.SessionLocal() as db:
        review=db.get(WoundPhotoAnalysis,eid);usage=db.scalar(select(billing.CreditUsage))
        assert review.state=='success' and review.model=='test-vision'
        assert usage.status=='success' and usage.credits==5 and usage.source_id is None
        assert db.scalar(select(func.count()).select_from(billing.CreditUsage))==1
        assert db.scalar(select(func.count()).select_from(web_app.WebDocument))==0
    page=b.get(f'/care/pets/{pid}');assert page.status_code==200
    assert '&lt;script&gt;bad()&lt;/script&gt;' in page.text and '<script>bad()' not in page.text
    assert 'private, no-store' in page.headers['Cache-Control']
    assert post(a,f'/care/pets/{pid}/members/{buid}/remove').status_code==303
    assert post(b,path).status_code==404 and b.get(f'/care/pets/{pid}').status_code==404


def test_concurrent_request_is_blocked_and_failed_model_refunds_retry():
    a,uid,pid=account();eid,_=entry(uid,pid);path=f'/care/photos/{eid}/analyse';storage.set_bot_setting('billing_live','1')
    def failing(**kwargs):
        assert post(a,path).status_code==409
        raise RuntimeError('timeout')
    with patch.object(web_app.client.responses,'create',side_effect=failing) as model:
        assert post(a,path).status_code==422 and model.call_count==1
    with storage.SessionLocal() as db:
        assert db.get(WoundPhotoAnalysis,eid).state=='failed'
        assert db.scalar(select(billing.CreditUsage)).status=='failed'
        before=billing.summary(db,uid)['balance']
    with patch.object(web_app.client.responses,'create',return_value=response()): assert post(a,path).status_code==303
    with storage.SessionLocal() as db: assert billing.summary(db,uid)['balance']==before-5


def test_malformed_model_response_refunds_and_does_not_save_partial_result():
    a,uid,pid=account();eid,_=entry(uid,pid)
    with patch.object(web_app.client.responses,'create',return_value=SimpleNamespace(output_text='not JSON')):
        assert post(a,f'/care/photos/{eid}/analyse').status_code==422
    with storage.SessionLocal() as db:
        assert db.get(WoundPhotoAnalysis,eid).result is None
        assert db.scalar(select(billing.CreditUsage)).status=='failed'


def test_family_revocation_during_ai_call_refunds_and_prevents_saved_answer():
    a,uid,pid=account();b,buid,_=account('family@example.com');invite(a,pid,b);eid,_=entry(uid,pid)
    def revoke(**kwargs):
        assert post(a,f'/care/pets/{pid}/members/{buid}/remove').status_code==303
        return response()
    with patch.object(web_app.client.responses,'create',side_effect=revoke):
        assert post(b,f'/care/photos/{eid}/analyse').status_code==404
    with storage.SessionLocal() as db:
        assert db.get(WoundPhotoAnalysis,eid).state=='failed'
        assert db.get(WoundPhotoAnalysis,eid).result is None
        assert db.scalar(select(billing.CreditUsage)).status=='failed'


def test_zero_balance_keeps_free_owner_warning_and_never_calls_ai():
    a,uid,pid=account();eid,_=entry(uid,pid,urgency='urgent',flags=['bleeding'])
    storage.set_bot_setting('billing_live','1');exhaust(uid)
    with patch.object(web_app.client.responses,'create') as model:
        r=post(a,f'/care/photos/{eid}/analyse');assert r.status_code==402
        assert 'Нужна экстренная ветеринарная помощь' in r.text;model.assert_not_called()
    with storage.SessionLocal() as db:
        assert not db.scalar(select(billing.CreditUsage))
        assert db.get(WoundPhotoAnalysis,eid).state=='failed'


def test_csrf_auth_and_chronic_or_missing_photo_are_rejected_before_ai():
    a,uid,pid=account();eid,_=entry(uid,pid)
    with patch.object(web_app.client.responses,'create') as model:
        assert a.post(f'/care/photos/{eid}/analyse').status_code==400
        assert web_app.app.test_client().post(f'/care/photos/{eid}/analyse').status_code==302
        b,_,_=account('outside@example.com');assert post(b,f'/care/photos/{eid}/analyse').status_code==404
        for changes in ({'kind':'chronic'},{'photo':None}):
            bad,_=entry(uid,pid,**changes);assert post(a,f'/care/photos/{bad}/analyse').status_code==400
        model.assert_not_called()


def test_expired_lease_recovery_and_old_entry_redirects_to_correct_page():
    a,uid,pid=account();eid,oid=entry(uid,pid,observed_at=datetime.utcnow()-timedelta(days=4))
    usage,_=billing.reserve(uid,'document','expired-wound-request',b'expired')
    with storage.SessionLocal() as db:
        db.add(WoundPhotoAnalysis(entry_id=eid,state='pending',lease='old',requested_by=uid,usage_id=usage.id,
            started_at=datetime.utcnow()-timedelta(minutes=11),prompt_version=wound.VERSION,language='ru'))
        db.commit()
    for i in range(31): entry(uid,pid,operation_id=oid,photo=None)
    with patch.object(web_app.client.responses,'create',return_value=response()): r=post(a,f'/care/photos/{eid}/analyse')
    assert r.status_code==303 and 'page=2' in r.location
    page=a.get(r.location).text;assert f'id="entry-{eid}"' in page and 'Что видно на фото' in page
    with storage.SessionLocal() as db: assert db.get(billing.CreditUsage,usage.id).status=='failed'


def test_doctor_readonly_consent_gate_and_english_ui():
    from test_doctor_portal import doctor
    a,uid,pid=account();eid,_=entry(uid,pid)
    with patch.object(web_app.client.responses,'create',return_value=response()): post(a,f'/care/photos/{eid}/analyse')
    d=doctor();path=f'/doctor/care/pets/{pid}';assert d.get(path).status_code==404
    with storage.SessionLocal() as db:
        db.add(CareSharing(pet_id=pid,doctor_enabled=True))
        db.add(storage.Consultation(user_id=uid,pet_id=pid,kind='consult_request',user_text='QA',assistant_text=''));db.commit()
    page=d.get(path);assert page.status_code==200 and 'Что видно на фото' in page.text
    assert 'data-wound-analysis' not in page.text
    d.set_cookie('language','en');assert 'Visible findings' in d.get(path).text
    post(a,f'/care/pets/{pid}/preferences');assert d.get(path).status_code==404


@pytest.mark.parametrize('caption',['Шов после операции, проверь фото',None])
async def test_real_telegram_pipeline_includes_wound_rules_even_without_caption(bot,ctx,update,raw,caption):
    data=image();ctx.bot.get_file.return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=data))
    item=SimpleNamespace(file_id='wound',file_unique_id='wound',file_size=len(data))
    await bot.media(update(photo=[item],caption=caption),ctx)
    call=next(call for call in raw.responses.calls if any(isinstance(x.get('content'),list) for x in call.get('input',[])))
    assert 'SURGICAL WOUND PHOTO REVIEW' in call['instructions']
    assert 'ONE observation' in call['instructions'] and 'No earlier photos' in call['instructions']


@pytest.mark.parametrize('text,word',[
    ('У собаки шов разошёлся','сегодня'),('Из раны сильное кровотечение','сейчас'),
    ('The surgical incision has opened','today'),('Heavy bleeding from the incision','now')])
def test_free_wound_warning(text,word):
    from safety_notice import free_safety_notice
    assert word in free_safety_notice(text)


@pytest.mark.parametrize('text',[
    'Шов не разошелся, сильного кровотечения нет','No bleeding from the wound',
    'Год назад шов разошелся. Сейчас всё хорошо', 'Что такое сильное кровотечение из раны?'])
def test_no_false_wound_emergency_for_denials_or_history(text):
    from safety_notice import free_safety_notice
    assert free_safety_notice(text)==''
