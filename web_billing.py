"""Owner billing pages and doctor-only manual payment review."""
import io
import secrets
from functools import wraps
from datetime import datetime
from flask import abort, flash, redirect, render_template, request, send_file, session, url_for
from sqlalchemy import func, select, text as sql_text
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
                    plans=b.PLANS, card_transfers=b.CARD_TRANSFERS, order_states=b.ORDER_STATES, money=b.money, billing_csrf=csrf(),
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
            if b.metered(db, uid):
                b.trial(db, uid)
            db.commit()
            info = b.summary(db, uid)
            methods = b.card_methods(db)
            selected_transfer = request.args.get('transfer', next(iter(methods), 'mir'))
            if selected_transfer not in b.CARD_TRANSFERS:
                abort(400)
            orders = db.scalars(select(b.PaymentOrder).where(b.PaymentOrder.user_id == uid).order_by(b.PaymentOrder.created_at.desc()).limit(50)).all()
            page = max(1, request.args.get('page', 1, type=int))
            usage = db.scalars(select(b.CreditUsage).where(b.CreditUsage.user_id == uid)
                    .order_by(b.CreditUsage.created_at.desc()).offset((page - 1) * 30).limit(31)).all()
            return render('billing', 'Тариф и оплаты', info=info, methods=methods, orders=orders,
                          usage=usage[:30], has_more=len(usage) > 30, page_number=page,
                          sales=b.live(), selected_transfer=selected_transfer, request_key=secrets.token_hex(16))

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
            receipt = db.get(b.PaymentReceipt, oid)
            if request.method == 'POST':
                action = request.form.get('action')
                if action == 'report' and order.status == 'awaiting':
                    reference = request.form.get('reference', '').strip()
                    if not 3 <= len(reference) <= 200:
                        flash('Укажите время перевода и имя отправителя или идентификатор транзакции.')
                        return redirect(url_for('billing_order', oid=oid), code=303)
                    upload = request.files.get('receipt')
                    if upload and upload.filename:
                        mime = (upload.mimetype or '').lower()
                        if mime not in {'image/jpeg', 'image/png', 'image/webp', 'application/pdf'}:
                            flash('Чек можно прикрепить как JPG, PNG, WEBP или PDF.')
                            return redirect(url_for('billing_order', oid=oid), code=303)
                        data = upload.read(8 * 1024 * 1024 + 1)
                        if not data or len(data) > 8 * 1024 * 1024:
                            flash('Файл чека должен быть не больше 8 МБ.')
                            return redirect(url_for('billing_order', oid=oid), code=303)
                        filename = upload.filename.replace('\\', '/').split('/')[-1][:255] or 'receipt'
                        receipt = b.PaymentReceipt(order_id=oid, filename=filename, mime_type=mime, data=data)
                        db.merge(receipt)
                    elif receipt is None:
                        flash('Прикрепите чек или скриншот перевода.')
                        return redirect(url_for('billing_order', oid=oid), code=303)
                    order.payment_reference = reference
                    order.status = 'review'
                    order.reported_at = datetime.utcnow()
                    from payment_notifications import PaymentNotice
                    db.add(PaymentNotice(order_id=order.id))
                elif action == 'cancel' and order.status == 'awaiting':
                    order.status = 'cancelled'
                elif action == 'report' and order.status in ('review', 'paid'):
                    pass
                else:
                    abort(409)
                db.commit()
                return redirect(url_for('billing_order', oid=oid), code=303)
            grant = db.scalar(select(b.CreditGrant).where(b.CreditGrant.origin == 'payment:' + oid))
            return render('billing_order', 'Заявка на оплату', order=order, grant=grant, receipt=receipt)

    @app.get('/billing/orders/<oid>/receipt')
    def billing_receipt(oid):
        doctor_id = app.extensions['doctor_identity']()
        with storage.SessionLocal() as db:
            order = db.get(b.PaymentOrder, oid)
            if not order:
                abort(404)
            if session.get('uid') != order.user_id and not doctor_id:
                abort(403)
            receipt = db.get(b.PaymentReceipt, oid)
            if not receipt:
                abort(404)
            return send_file(io.BytesIO(receipt.data), mimetype=receipt.mime_type,
                             download_name=receipt.filename, as_attachment=False)

    @app.route('/doctor/payments', methods=['GET', 'POST'])
    @doctor
    def doctor_payments():
        if request.method == 'POST':
            check_csrf()
            enabled = request.form.get('enabled') == '1'
            with storage.SessionLocal() as db:
                if enabled and not b.card_methods(db):
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
            return render('doctor_payments', 'Оплаты и пакеты', rows=rows[:30], has_more=len(rows) > 30,
                          page_number=page, status=status, review_count=count, totals=totals,
                          configured=b.card_methods(db, include_disabled=True), sales=b.live())

    @app.post('/doctor/payments/methods')
    @doctor
    def payment_method_add():
        check_csrf()
        try:
            b.save_card_method(request.form.get('transfer'), request.form.get('currency'),
                               request.form.get('instructions', '').strip(),
                               {key: request.form.get('price_' + key, '') for key in b.PLANS})
        except b.BillingError as error:
            flash(str(error))
            return redirect(url_for('doctor_payments'), code=303)
        flash('Реквизиты сохранены. Новые заявки будут использовать эти данные.')
        return redirect(url_for('doctor_payments'), code=303)

    @app.post('/doctor/payments/methods/<int:mid>/toggle')
    @doctor
    def payment_method_toggle(mid):
        check_csrf()
        with storage.SessionLocal() as db:
            if db.bind.dialect.name == 'postgresql':
                db.execute(sql_text('SELECT pg_advisory_xact_lock(:key)'), {'key': 728190424})
            method = db.get(b.PaymentMethod, mid, with_for_update=True)
            if not method:
                abort(404)
            enabled = request.form.get('enabled') == '1'
            route = b.method_route(method.name)
            names = b.route_names(route) if route else (method.name,)
            latest = db.scalar(select(b.PaymentMethod.id).where(b.PaymentMethod.name.in_(names)).order_by(b.PaymentMethod.id.desc()).limit(1))
            if enabled and (mid != latest or not b.method_prices(db, method)):
                abort(409)
            method.enabled = enabled
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
                receipt = db.get(b.PaymentReceipt, oid)
                return render('doctor_payment', 'Проверка оплаты', order=order, owner=user, account=account, grant=grant, receipt=receipt)
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
