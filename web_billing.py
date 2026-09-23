"""Owner billing pages and doctor-only manual payment review."""
import secrets
from functools import wraps
from datetime import datetime
from flask import abort, flash, redirect, render_template, request, session, url_for
from sqlalchemy import func, select
import storage
import billing as b
from consultation_cases import STATUSES
from web_i18n import t


def install(app, WebAccount):
    import billing_i18n  # Registers the English copy alongside the billing module.
    b.init_schema()

    @app.context_processor
    def billing_helpers():
        def metered():
            if not session.get('uid'):
                return False
            with storage.SessionLocal() as db:
                return b.metered(db, session['uid'])
        return {'billing_request_key': lambda: secrets.token_hex(16), 'billing_metered': metered}

    def csrf():
        session.setdefault('billing_csrf', secrets.token_urlsafe(32))
        return session['billing_csrf']

    def owner(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get('uid'):
                if request.method != 'GET':
                    abort(401)
                return redirect(url_for('login', next=request.path))
            return fn(*args, **kwargs)
        return wrapped

    def doctor(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not app.extensions['doctor_identity']():
                if request.method != 'GET':
                    abort(403)
                return redirect(url_for('doctor_login', next=request.path))
            return fn(*args, **kwargs)
        return wrapped

    def check_csrf():
        if not secrets.compare_digest(request.form.get('billing_csrf', ''), csrf()):
            abort(400)

    def render(page, title, **kwargs):
        session.setdefault('doctor_csrf', secrets.token_urlsafe(32))
        response = app.make_response(render_template('section.html', page=page, title=t(title),
                    plans=b.PLANS, order_states=b.ORDER_STATES, money=b.money, billing_csrf=csrf(),
                    statuses=STATUSES, doctor_csrf=session['doctor_csrf'], now=datetime.utcnow(), **kwargs))
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.get('/billing')
    @owner
    def billing_page():
        uid = session['uid']
        with storage.SessionLocal() as db:
            b.lock_user(db, uid)
            b.release_stale(db, uid)
            if b.live():
                b.trial(db, uid)
            db.commit()
            info = b.summary(db, uid)
            methods = db.scalars(select(b.PaymentMethod).where(b.PaymentMethod.enabled.is_(True)).order_by(b.PaymentMethod.id)).all()
            orders = db.scalars(select(b.PaymentOrder).where(b.PaymentOrder.user_id == uid).order_by(b.PaymentOrder.created_at.desc()).limit(50)).all()
            page = max(1, request.args.get('page', 1, type=int))
            usage = db.scalars(select(b.CreditUsage).where(b.CreditUsage.user_id == uid)
                    .order_by(b.CreditUsage.created_at.desc()).offset((page - 1) * 30).limit(31)).all()
            return render('billing', 'Тариф и оплаты', info=info, methods=methods, orders=orders,
                          usage=usage[:30], has_more=len(usage) > 30, page_number=page,
                          sales=b.live(), request_key=secrets.token_hex(16))

    @app.post('/billing/orders')
    @owner
    def billing_create():
        check_csrf()
        try:
            oid = b.create_order(session['uid'], request.form.get('plan'), request.form.get('method_id', type=int), request.form.get('request_key'))
        except b.BillingError as error:
            flash(str(error))
            return redirect(url_for('billing_page'), code=303)
        return redirect(url_for('billing_order', oid=oid), code=303)

    @app.route('/billing/orders/<oid>', methods=['GET', 'POST'])
    @owner
    def billing_order(oid):
        with storage.SessionLocal() as db:
            if request.method == 'POST':
                check_csrf()
                b.lock_user(db, session['uid'])
            order = db.scalar(select(b.PaymentOrder).where(b.PaymentOrder.id == oid, b.PaymentOrder.user_id == session['uid']))
            if not order:
                abort(404)
            if request.method == 'POST':
                action = request.form.get('action')
                if action == 'report' and order.status == 'awaiting':
                    reference = request.form.get('reference', '').strip()
                    if not 3 <= len(reference) <= 200:
                        flash('Укажите время перевода и имя отправителя или идентификатор транзакции.')
                        return redirect(url_for('billing_order', oid=oid), code=303)
                    order.payment_reference = reference
                    order.status = 'review'
                    order.reported_at = datetime.utcnow()
                elif action == 'cancel' and order.status == 'awaiting':
                    order.status = 'cancelled'
                elif action == 'report' and order.status in ('review', 'paid'):
                    pass
                else:
                    abort(409)
                db.commit()
                return redirect(url_for('billing_order', oid=oid), code=303)
            grant = db.scalar(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:' + oid))
            return render('billing_order', 'Заявка на оплату', order=order, grant=grant)

    @app.route('/doctor/payments', methods=['GET', 'POST'])
    @doctor
    def doctor_payments():
        if request.method == 'POST':
            check_csrf()
            enabled = request.form.get('enabled') == '1'
            with storage.SessionLocal() as db:
                if enabled and not db.scalar(select(b.PaymentMethod.id).where(b.PaymentMethod.enabled.is_(True)).limit(1)):
                    flash('Сначала добавьте реквизиты для оплаты.')
                    return redirect(url_for('doctor_payments'), code=303)
            storage.set_bot_setting('billing_live', '1' if enabled else '0')
            flash('Настройки оплаты сохранены.')
            return redirect(url_for('doctor_payments'), code=303)
        status = request.args.get('status', 'review')
        if status and status not in b.ORDER_STATES:
            abort(400)
        page = max(1, request.args.get('page', 1, type=int))
        with storage.SessionLocal() as db:
            query = select(b.PaymentOrder, storage.User, WebAccount).join(storage.User, storage.User.id == b.PaymentOrder.user_id).outerjoin(WebAccount, WebAccount.user_id == storage.User.id)
            if status:
                query = query.where(b.PaymentOrder.status == status)
            rows = db.execute(query.order_by(b.PaymentOrder.created_at.desc()).offset((page - 1) * 30).limit(31)).all()
            count = db.scalar(select(func.count(b.PaymentOrder.id)).where(b.PaymentOrder.status == 'review'))
            totals = db.execute(select(b.PaymentOrder.currency, func.sum(b.PaymentOrder.amount_minor)).where(b.PaymentOrder.status == 'paid').group_by(b.PaymentOrder.currency)).all()
            methods = db.scalars(select(b.PaymentMethod).order_by(b.PaymentMethod.id)).all()
            return render('doctor_payments', 'Оплаты и пакеты', rows=rows[:30], has_more=len(rows) > 30,
                          page_number=page, status=status, review_count=count, totals=totals, methods=methods, sales=b.live())

    @app.post('/doctor/payments/methods')
    @doctor
    def payment_method_add():
        check_csrf()
        name = request.form.get('name', '').strip()
        currency = request.form.get('currency')
        network = request.form.get('network', '').strip()
        instructions = request.form.get('instructions', '').strip()
        if not 2 <= len(name) <= 80 or currency not in ('RUB', 'USD', 'USDT') or not 10 <= len(instructions) <= 2000 or len(network) > 80 or (currency == 'USDT' and not network):
            flash('Проверьте название, валюту и реквизиты. Для USDT обязательно укажите сеть.')
            return redirect(url_for('doctor_payments'), code=303)
        with storage.SessionLocal() as db:
            if db.scalar(select(func.count(b.PaymentMethod.id))) >= 20:
                abort(409)
            db.add(b.PaymentMethod(name=name, currency=currency, network=network if currency == 'USDT' else None, instructions=instructions))
            db.commit()
        flash('Способ оплаты добавлен.')
        return redirect(url_for('doctor_payments'), code=303)

    @app.post('/doctor/payments/methods/<int:mid>/toggle')
    @doctor
    def payment_method_toggle(mid):
        check_csrf()
        with storage.SessionLocal() as db:
            method = db.get(b.PaymentMethod, mid)
            if not method:
                abort(404)
            method.enabled = request.form.get('enabled') == '1'
            db.commit()
        return redirect(url_for('doctor_payments'), code=303)

    @app.route('/doctor/payments/<oid>', methods=['GET', 'POST'])
    @doctor
    def doctor_payment(oid):
        with storage.SessionLocal() as db:
            order = db.get(b.PaymentOrder, oid)
            if not order:
                abort(404)
            uid = order.user_id
            user = db.get(storage.User, uid)
            account = db.scalar(select(WebAccount).where(WebAccount.user_id == uid))
            if request.method == 'GET':
                grant = db.scalar(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:' + oid))
                return render('doctor_payment', 'Проверка оплаты', order=order, owner=user, account=account, grant=grant)
        check_csrf()
        action = request.form.get('action')
        if action == 'confirm':
            if request.form.get('received') != '1':
                flash('Подтвердите поступление точной суммы в банке или кошельке.')
                return redirect(url_for('doctor_payment', oid=oid), code=303)
            try:
                b.confirm_order(uid, oid, app.extensions['doctor_identity']())
            except b.BillingError as error:
                flash(str(error))
                return redirect(url_for('doctor_payment', oid=oid), code=303)
            flash('Оплата подтверждена. Баллы начислены один раз.')
        elif action == 'reject':
            reason = request.form.get('reason', '').strip()
            if not 3 <= len(reason) <= 500:
                flash('Укажите причину для владельца.')
                return redirect(url_for('doctor_payment', oid=oid), code=303)
            with storage.SessionLocal() as db:
                b.lock_user(db, uid)
                order = db.get(b.PaymentOrder, oid)
                if order.status != 'review':
                    abort(409)
                order.status = 'rejected'
                order.decision_note = reason
                order.checked_by = str(app.extensions['doctor_identity']())
                db.commit()
        else:
            abort(400)
        return redirect(url_for('doctor_payment', oid=oid), code=303)
