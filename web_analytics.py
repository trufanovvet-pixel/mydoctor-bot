"""Analytics routes reuse the doctor's existing server-side identity check."""
from datetime import datetime
import os
import re
import secrets
import time
from functools import wraps
from collections import OrderedDict
from threading import Lock
from itsdangerous import URLSafeTimedSerializer, BadSignature
from flask import abort, g, jsonify, redirect, render_template, request, session
from consultation_cases import STATUSES
import analytics as a
import billing
from web_i18n import t


def install(app):
    a.migrate()
    a.install_hooks()
    # Exercise production SQL before a worker becomes healthy (no writes/PII).
    check = a.report('today', timezone=os.getenv('MYDOCTOR_ANALYTICS_TIMEZONE', 'Asia/Bangkok'))
    app.logger.warning('Analytics ready; active_today=%s; registrations_total=%s; confirmed_payments_today=%s',
                       check['active_users'], check['registered_total'], check['payments'])

    signer = URLSafeTimedSerializer(app.secret_key, salt='mydoctor-analytics-v1')
    recent = OrderedDict()
    rate_lock = Lock()
    cookie_name = 'mydoctor_analytics'
    # Telemetry has its own cookie. A late beacon must never overwrite a newer
    # login/logout cookie, including Flask's permanent-session refresh.
    original_should_set_cookie = app.session_interface.should_set_cookie
    def should_set_cookie(app_, session_):
        return False if request.endpoint == 'analytics_collect' else original_should_set_cookie(app_, session_)
    app.session_interface.should_set_cookie = should_set_cookie

    @app.before_request
    def analytics_context():
        if request.endpoint == 'static' or request.path.startswith(('/doctor', '/api/doctor', '/health', '/app-icon')):
            g.analytics_token = a.context.set({})
            return
        now = time.time()
        uid = session.get('uid')
        try:
            state = signer.loads(request.cookies.get(cookie_name, ''), max_age=30*86400)
            if not isinstance(state, dict): state = {}
        except BadSignature:
            state = {}
        g.analytics_previous_sid = state.get('sid')
        switching = bool(uid and request.method == 'POST' and request.endpoint in ('login','register','telegram_login'))
        if not state.get('sid') or now-state.get('seen',0)>1800 or (state.get('uid') and state['uid'] != uid) or switching:
            state = {'sid':secrets.token_hex(16)}
        state.update(seen=now, uid=uid)
        g.analytics_state = state
        reported_source = request.headers.get('X-MyDoctor-Source')
        if not reported_source and request.method == 'POST' and request.mimetype in ('application/x-www-form-urlencoded','multipart/form-data'):
            reported_source = request.form.get('analytics_source')
        source = 'app' if (reported_source or request.cookies.get('mydoctor_display')) == 'app' else 'web'
        sid = state['sid']
        g.analytics_uid_before = uid
        g.analytics_token = a.context.set({'source':source, 'session_id':sid, 'anonymous_id':sid})

    @app.context_processor
    def analytics_template():
        sid = a.context.get().get('session_id')
        return {'analytics_csrf':signer.dumps({'csrf_sid':sid}) if sid else ''}

    @app.after_request
    def analytics_login(response):
        uid = session.get('uid')
        if request.endpoint in ('login', 'register', 'telegram_login') and request.method == 'POST' and response.status_code in (302,303) and uid:
            ctx = a.context.get()
            sid = ctx.get('session_id')
            if sid:
                a.bind_identity(uid, sid)
            a.track('login', uid, details={'method':'telegram' if request.endpoint == 'telegram_login' else 'password'})
        state = getattr(g, 'analytics_state', None)
        if state:
            state['uid'] = uid
            response.set_cookie(cookie_name, signer.dumps(state), max_age=30*86400, secure=True, httponly=True, samesite='Lax')
        return response

    @app.teardown_request
    def analytics_end(error=None):
        token = getattr(g, 'analytics_token', None)
        if token is not None:
            a.context.reset(token)
            del g.analytics_token

    @app.post('/api/analytics/events')
    def analytics_collect():
        if request.content_length and request.content_length > 2048: abort(413)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict): abort(400)
        try:
            proof = signer.loads(str(payload.get('csrf','')), max_age=86400)
        except BadSignature:
            abort(403)
        if not isinstance(proof,dict) or not proof.get('csrf_sid') or proof['csrf_sid'] not in (a.context.get().get('session_id'), getattr(g,'analytics_previous_sid',None)): abort(403)
        if request.headers.get('Sec-Fetch-Site') == 'cross-site': abort(403)
        page = payload.get('page')
        if page not in a.NAVIGATION: abort(400)
        if app.extensions['doctor_identity'](): return '', 204
        eid = payload.get('event_id', '')
        if not isinstance(eid, str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,64}', eid): abort(400)
        # Limit per-session navigation collection without slowing business endpoints.
        now = time.time()
        sid = a.context.get()['session_id']
        with rate_lock:
            times = [x for x in recent.pop(sid, []) if now-x<60]
            recent[sid] = (times + [now])[-60:]
            while len(recent)>10000: recent.popitem(last=False)
            if len(times)>=60: return '', 429
        source = 'app' if payload.get('source') == 'app' else 'web'
        ctx = {**a.context.get(), 'source':source}
        token = a.context.set(ctx)
        try:
            uid = session.get('uid')
            sid = ctx['session_id']
            rows = [a.row('app_open', uid, key=f'session:{sid}:{source}')]
            if a.NAVIGATION[page]:
                rows.append(a.row(a.NAVIGATION[page], uid, key=f'nav:{sid}:{eid}'))
            a.track_batch(rows)
        finally:
            a.context.reset(token)
        return '', 204

    def doctor_only(fn):
        @wraps(fn)
        def guarded(*args, **kwargs):
            if not app.extensions['doctor_identity']():
                if request.path.startswith('/api/'): abort(403)
                session['doctor_next'] = '/doctor/analytics'
                return redirect('/doctor/login')
            return fn(*args, **kwargs)
        return guarded

    def data():
        period = request.args.get('period','today')
        source = request.args.get('source') or None
        if period not in ('today','7','30','all') or source not in (*a.SOURCES,None,'unknown'): abort(400)
        timezone = os.getenv('MYDOCTOR_ANALYTICS_TIMEZONE', 'Asia/Bangkok')
        return a.report(period, source, timezone)

    @app.get('/api/doctor/analytics')
    @doctor_only
    def analytics_api():
        response = jsonify(data())
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    @app.get('/doctor/analytics')
    @doctor_only
    def doctor_analytics():
        session.setdefault('doctor_csrf', secrets.token_urlsafe(32))
        response = app.make_response(render_template('section.html', page='doctor_analytics', title=t('Аналитика'),
             stats=data(), statuses=STATUSES, doctor_csrf=session['doctor_csrf'], money=billing.money))
        response.headers['Cache-Control'] = 'private, no-store'
        return response
