"""Real routes + isolated DB; no provider calls, bank transfers or production data."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import re
import pytest
from sqlalchemy import select, func, event
import analytics as a
import storage
import billing as b
with patch('openai.OpenAI'):
    import web_app
from test_web_app import reset_db, register
from test_doctor_portal import doctor
from test_billing import owner, order_for, report as report_payment
from test_telegram_billing import setup_sbp, sign_in
import web_auth


@pytest.fixture(autouse=True)
def clean():
    reset_db()
    a.migrate()


def collect(c, page='/', source='web', key='visit-000000000001'):
    html=c.get(page).text
    csrf=re.search('name="analytics-csrf" content="([^"]*)"',html).group(1)
    return c.post('/api/analytics/events', json={'csrf':csrf,'page':page,'source':source,'event_id':key})


def test_anonymous_visit_links_at_registration_and_channels_deduplicate():
    c=web_app.app.test_client()
    assert collect(c).status_code==204
    assert a.report('today')['anonymous_visitors']==1
    register(c)
    assert collect(c,'/pets',key='visit-000000000002').status_code==204
    c.set_cookie('mydoctor_display','app')
    assert collect(c,'/assistant','app','visit-000000000003').status_code==204
    stats=a.report('today')
    assert stats['active_users']==1 and stats['anonymous_visitors']==0
    assert stats['channels']['web']==1 and stats['channels']['app']==1
    assert stats['new_users']==1
    assert [s['users'] for s in stats['funnel'][:2]]==[1,1]
    assert a.report('today','web')['active_users']==1
    assert a.report('today','app')['active_users']==1


def test_dashboard_permissions_periods_sources_and_manifest():
    c,uid=owner(); staff=doctor()
    assert c.get('/doctor/analytics').status_code==302
    assert c.get('/api/doctor/analytics').status_code==403
    assert web_app.app.test_client().get('/api/doctor/analytics').status_code==403
    for period in ('today','7','30','all'):
        result=staff.get('/api/doctor/analytics',query_string={'period':period})
        assert result.status_code==200 and result.json['period']==period
        assert 'no-store' in result.headers['Cache-Control']
        assert staff.get('/doctor/analytics',query_string={'period':period}).status_code==200
    assert staff.get('/api/doctor/analytics?period=bad').status_code==400
    assert staff.get('/api/doctor/analytics?source=bad').status_code==400
    assert 'Аналитика' in staff.get('/doctor/ai').text
    assert '/doctor/analytics' not in c.get('/dashboard').text
    assert c.get('/doctor.webmanifest').json['start_url']=='/doctor/dashboard'
    assert c.get('/manifest.webmanifest').json['start_url']=='/dashboard'


def test_collection_cannot_forge_payments_uid_time_or_metadata():
    c,uid=owner()
    html=c.get('/dashboard').text
    csrf=re.search('name="analytics-csrf" content="([^"]*)"',html).group(1)
    assert c.post('/api/analytics/events',json={'csrf':'bad','page':'/'}).status_code==403
    assert c.post('/api/analytics/events',json={'csrf':csrf,'page':'payment_success','event_id':'test-key-12345678'}).status_code==400
    data={'csrf':csrf,'page':'/pets','event_id':'test-key-12345678','event_name':'payment_success','user_id':999,'timestamp':'1900','metadata':{'email':'DO NOT STORE'}}
    assert c.post('/api/analytics/events',json=data).status_code==204
    assert c.post('/api/analytics/events',json=data).status_code==204
    with storage.SessionLocal() as db:
        rows=db.scalars(select(a.AnalyticsEvent).where(a.AnalyticsEvent.event_name=='pets_open')).all()
        assert len(rows)==1 and rows[0].user_id==uid and rows[0].details=={}
        assert not db.scalar(select(a.AnalyticsEvent).where(a.AnalyticsEvent.event_name=='payment_success'))


def test_business_events_only_on_committed_real_operations_and_ai_replay():
    c,uid=owner()
    c.post('/pets',data={'name':'','species':'собака'})
    assert a.report('today')['events'].get('pet_created',0)==0
    c.set_cookie('mydoctor_display','app')
    c.post('/pets',data={'name':'QA pet','species':'собака'})
    response=SimpleNamespace(output_text='Test answer',model='test',usage=None)
    with patch.object(web_app.client.responses,'create',return_value=response) as provider:
        payload={'message':'Test question','request_key':'qa-request-one'}
        assert c.post('/api/chat',json=payload).status_code==200
        assert c.post('/api/chat',json=payload).status_code==200
        assert provider.call_count==1
    stats=a.report('today','app')
    assert stats['ai_questions']==1 and stats['events']['pet_created']==1
    with storage.SessionLocal() as db:
        db.add(storage.Pet(user_id=uid,name='rollback',species='cat'));db.flush();db.rollback()
    assert a.report('today')['events']['pet_created']==1


def test_safe_payment_confirmation_uses_ledger_paid_at_and_original_source():
    c,uid=owner();mid=setup_sbp()
    c.set_cookie('mydoctor_display','app')
    oid,_=order_for(c,mid)
    assert a.report('today')['payments']==0
    assert a.report('today')['events']['payment_started']==1
    report_payment(c,oid)
    b.confirm_order(uid,oid,123)
    b.confirm_order(uid,oid,123)
    stats=a.report('today','app')
    assert stats['payments']==1 and stats['first_payers']==1
    assert stats['revenue']==[{'currency':'RUB','amount_minor':50000,'count':1,'average_minor':50000}]
    assert stats['plans'][0]['plan']=='Старт'
    assert stats['events']['payment_success']==1
    assert a.report('today','telegram')['payments']==0
    with storage.SessionLocal() as db:
        db.get(b.PaymentOrder,oid).paid_at=datetime.utcnow()-timedelta(days=9);db.commit()
    assert a.report('today')['payments']==0
    assert a.report('30')['payments']==1


def test_payment_failure_and_currency_never_sum_as_rubles():
    c,uid=owner();rub=setup_sbp()
    usd=b.save_card_method('mastercard','USD','TEST ONLY details',{'start':'7','care':'14','family':'29'})
    orders=[]
    for mid in (rub,usd):
        oid,_=order_for(c,mid);report_payment(c,oid);b.confirm_order(uid,oid,123);orders.append(oid)
    # A new request key for the same plan must create a distinct order.
    oid=b.create_order(uid,'care',rub,'rejected-unique')
    with storage.SessionLocal() as db:
        db.get(b.PaymentOrder,oid).status='rejected';db.commit()
    stats=a.report('today')
    assert stats['payments']==2
    assert {x['currency']:x['amount_minor'] for x in stats['revenue']}=={'RUB':50000,'USD':700}
    assert stats['events']['payment_failed']==1


def test_ordered_funnel_excludes_out_of_order_steps():
    now=datetime.utcnow(); start=now-timedelta(minutes=20)
    with storage.engine.begin() as conn:
        conn.execute(a.AnalyticsEvent.__table__.delete())
        conn.execute(a.AnalyticsState.__table__.update().values(value=start.isoformat()))
    with storage.SessionLocal() as db: uid=db.scalar(select(storage.User.id))
    uid=storage.ensure_user(800)['id']
    with storage.engine.begin() as conn:
        conn.execute(a.AnalyticsEvent.__table__.delete())
        events=[a.row(name,uid,key='ordered-'+name,at=start+timedelta(minutes=i),source='web',historical=True) for i,(name,label) in enumerate(a.FUNNEL)]
        a.insert_rows(conn,events)
    assert [s['users'] for s in a.report('today',now=now)['funnel']]==[1]*7
    with storage.engine.begin() as conn:
        conn.execute(a.AnalyticsEvent.__table__.update().where(a.AnalyticsEvent.event_name=='pricing_open').values(timestamp=start-timedelta(seconds=1)))
    assert [s['users'] for s in a.report('today',now=now)['funnel']]==[1,1,1,1,0,0,0]


def test_retention_and_period_timezone_boundaries():
    uid=storage.ensure_user(801)['id'];now=datetime(2026,9,26,19,30)
    since=now-timedelta(days=10)
    with storage.engine.begin() as conn:
        conn.execute(a.AnalyticsEvent.__table__.delete())
        conn.execute(a.AnalyticsState.__table__.update().values(value=since.isoformat()))
        rows=[]
        for n,hours in enumerate((48,2,0.5)):
            r=a.row('app_open',uid,key='retention-'+str(n),at=now-timedelta(hours=hours),source='web',historical=True)
            r['session_id']='session-'+str(n);rows.append(r)
        a.insert_rows(conn,rows)
    # Bangkok today starts at 17:00 UTC; both recent sessions fall on its new day.
    stats=a.report('today',timezone='Asia/Bangkok',now=now)
    assert stats['active_users']==1 and stats['repeat_users']==1 and stats['returning_users']==1
    assert len(stats['series'])==3 and stats['series'][0]['label']=='2026-09-27 00:00'
    assert a.period_bounds('7','Asia/Bangkok',now)[0]==datetime(2026,9,20,17)
    assert a.period_bounds('30','Asia/Bangkok',now)[0]==datetime(2026,8,28,17)


def test_historical_backfill_idempotent_no_fabricated_visits_or_duplicate_ai():
    with storage.engine.begin() as conn:
        conn.execute(a.AnalyticsState.__table__.delete())
    uid=storage.ensure_user(802)['id'];past=datetime.utcnow()-timedelta(days=2)
    with storage.SessionLocal() as db:
        db.add(storage.Consultation(user_id=uid,kind='chat',user_text='private',assistant_text='answer',created_at=past))
        db.add(storage.Consultation(user_id=uid,kind='chat',user_text='duplicate',assistant_text='answer',created_at=past+timedelta(hours=2)))
        db.add(b.CreditUsage(id='historic',user_id=uid,request_key='tg-802-1',fingerprint='x',kind='chat',status='success',created_at=past+timedelta(hours=1)))
        db.commit()
    with storage.engine.begin() as conn: conn.execute(a.AnalyticsEvent.__table__.delete())
    a.migrate();a.migrate()
    stats=a.report('all')
    assert stats['ai_questions']==2 and stats['events'].get('app_open',0)==0
    assert stats['repeat_users']==0 and all(s['users']==0 for s in stats['funnel'])


@pytest.mark.asyncio
async def test_telegram_events_and_same_account_web_login(bot,update,ctx):
    await bot.message(update('🐾 Мои питомцы'),ctx)
    await bot.message(update('🛡 Профилактика'),ctx)
    await bot.payment_command(update('/pay'),ctx)
    stats=a.report('today','telegram')
    assert stats['active_users']==1 and stats['events']['pets_open']==1
    assert stats['events']['prevention_open']==1 and stats['events']['pricing_open']==1
    assert [s['users'] for s in stats['funnel'][:2]]==[1,1]
    client=web_app.app.test_client()
    assert sign_in(client,web_auth.create_token(100)).status_code==303
    collect(client,'/billing',key='visit-web-tg-00001')
    assert a.report('today')['active_users']==1


def test_event_failure_does_not_break_business_transaction(monkeypatch):
    def fail(*args,**kwargs): raise RuntimeError('analytics-only problem')
    monkeypatch.setattr(a,'insert_rows',fail)
    user=storage.ensure_user(803)
    pet=storage.add_pet(803,'Saved','cat',None,None,None,None)
    with storage.SessionLocal() as db:
        assert db.get(storage.User,user['id']) and db.get(storage.Pet,pet['id'])


def test_document_upload_and_consultation_record_exactly_once():
    import io
    from test_web_consultation import form_client
    c,data=form_client()
    collect(c,'/analyses',key='document-open-00001')
    collect(c,'/consultation',key='consultation-open-1')
    response=SimpleNamespace(output_text='Test document answer',model='test',usage=None)
    with patch.object(web_app.client.responses,'create',return_value=response):
        result=c.post('/documents',data={'file':(io.BytesIO(b'test image'),'qa.png'),'request_key':'document-request-1'},content_type='multipart/form-data')
        assert result.status_code==302
    page=c.get('/consultation').text
    data['request_key']=re.search('name="request_key" value="([^"]*)"',page).group(1)
    for _ in range(2): assert c.post('/consultation',data=data).status_code==303
    stats=a.report('today')
    assert stats['events']['analysis_uploaded']==1
    assert stats['events']['analyses_open']==1 and stats['events']['consultation_open']==1
    assert stats['consultations']==1


def test_readonly_doctor_navigation_does_not_contaminate_user_metrics():
    staff=doctor()
    html=staff.get('/how-it-works').text
    csrf=re.search('name="analytics-csrf" content="([^"]*)"',html).group(1)
    assert staff.post('/api/analytics/events',json={'csrf':csrf,'page':'/','event_id':'doctor-ignored-123'}).status_code==204
    assert a.report('today')['events'].get('app_open',0)==0


def test_telemetry_response_never_overwrites_authentication_cookie():
    c,uid=owner()
    with c.session_transaction() as state: state.permanent=True
    response=collect(c)
    assert response.status_code==204
    assert not any(value.startswith(web_app.app.config['SESSION_COOKIE_NAME']+'=') for value in response.headers.getlist('Set-Cookie'))
    with c.session_transaction() as state: assert state['uid']==uid
