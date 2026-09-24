import io
import os
import secrets
from datetime import datetime
from functools import wraps
from flask import g,request,session,redirect,url_for,render_template,abort,flash,send_file
from sqlalchemy import select,func,or_,update
import storage
import billing
from consultation_cases import ConsultationCase,RequestStatusEvent,RequestAttachment,STATUSES,TYPES,request_files,attachment_content
from owner_profile import ConsultationContact
from doctor_access import DoctorAccess,DoctorWebAccount,consume_doctor_link,doctor_identity,hashed,doctor_destination,telegram_login_url,DOCTOR_COOKIE,REMEMBER_SECONDS,establish_doctor_session
from web_i18n import t
from consultation_chat import ConsultationMessage, MessageFile, thread_context, unread_counts


def install(app,WebDocument):
    storage.Base.metadata.create_all(storage.engine)

    @app.before_request
    def canonical_doctor_origin():
        # Only the two known aliases of THIS service; never trust Host as a target.
        canonical = os.getenv('MYDOCTOR_CANONICAL_ORIGIN', '').rstrip('/')
        aliases = {'mydoctor-web-production.up.railway.app', 'mydoctor-web-production-a7e2.up.railway.app'}
        if canonical == 'https://mydoctor.vet' and request.host in aliases and request.path.startswith('/doctor'):
            if request.method in ('GET', 'HEAD'):
                response = redirect(canonical + request.full_path.rstrip('?'), code=302)
            else:
                # Never forward credentials or mutations across origins.
                response = redirect(canonical + '/doctor/login', code=303)
            response.headers['Cache-Control'] = 'private, no-store'
            response.headers['Referrer-Policy'] = 'no-referrer'
            return response

    def identity():
        if not hasattr(g,'doctor_identity'):
            g.doctor_identity=None
            for grant in (request.cookies.get(DOCTOR_COOKIE),session.get('doctor_grant')):
                user=doctor_identity(grant)
                if user:
                    g.doctor_identity=user;g.doctor_grant=grant
                    break
        return g.doctor_identity

    app.extensions['doctor_identity']=identity

    @app.context_processor
    def doctor_context():return {'is_doctor':bool(identity())}

    def require_doctor(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            if not identity():
                if request.method!='GET':abort(403)
                session['doctor_next']=request.path
                return redirect(url_for('doctor_login'))
            return fn(*args,**kwargs)
        return wrapped

    def csrf():
        session.setdefault('doctor_csrf',secrets.token_urlsafe(32))
        return session['doctor_csrf']

    def check_csrf():
        if not secrets.compare_digest(request.form.get('doctor_csrf',''),csrf()):abort(400)

    def login_csrf_valid():
        return bool(session.get('doctor_csrf')) and secrets.compare_digest(
            request.form.get('doctor_csrf', ''), session['doctor_csrf'])

    def render(page,title,**kw):
        if page=='doctor_login':
            target=doctor_destination(session.get('doctor_next'))
            kw.setdefault('telegram_url',telegram_login_url(target))
        response=app.make_response(render_template('section.html',page=page,title=t(title),statuses=STATUSES,consultation_types=TYPES,doctor_csrf=csrf(),**kw))
        response.headers['Cache-Control']='private, no-store'
        response.headers['Referrer-Policy']='no-referrer'
        return response

    @app.route('/doctor/login', methods=['GET', 'POST'])
    def doctor_login():
        if request.args.get('next'):
            session['doctor_next']=doctor_destination(request.args['next'])
        if identity():return redirect(doctor_destination(session.pop('doctor_next',None)))
        if request.method == 'POST':
            if not login_csrf_valid():
                return render('doctor_login', 'Вход для врача', error='Форма входа обновлена после смены сеанса. Введите email и пароль ещё раз.'), 400
            from web_app import WebAccount
            from werkzeug.security import check_password_hash
            email = request.form.get('email', '').strip().lower()
            from doctor_access import allow_doctor_password_attempt
            if not allow_doctor_password_attempt(email):
                return render('doctor_login', 'Вход для врача', error='Слишком много попыток. Повторите через 15 минут.'), 429
            with storage.SessionLocal() as db:
                account = db.scalar(select(WebAccount).where(WebAccount.email == email))
                valid = account and check_password_hash(account.password_hash, request.form.get('password', ''))
                if valid and establish_doctor_session(account.user_id, request.form.get('remember') == '1'):
                    from web_push import revoke_browser_subscriptions
                    revoke_browser_subscriptions('owner')
                    session['uid'] = account.user_id
                    session.permanent = request.form.get('remember') == '1'
                    return redirect(doctor_destination(session.pop('doctor_next', None)), code=303)
                if valid:
                    return render('doctor_login', 'Вход для врача',
                        needs_link=True, telegram_url=telegram_login_url('/doctor/account'),
                        error='Email и пароль верны. Этот аккаунт ещё не связан с кабинетом врача. Подтвердите Telegram, затем привяжите этот же email на следующем экране.'), 403
            return render('doctor_login', 'Вход для врача', error='Не удалось войти. Проверьте данные и доступ врача. Для первого входа используйте Telegram.'), 403
        return render('doctor_login','Вход для врача')

    @app.route('/doctor/account', methods=['GET', 'POST'])
    @require_doctor
    def doctor_account():
        if request.method == 'POST':
            check_csrf()
            from web_app import WebAccount
            from werkzeug.security import check_password_hash
            from doctor_access import allow_doctor_password_attempt
            if not allow_doctor_password_attempt(request.form.get('email', '')):
                return render('doctor_account', 'Вход врача по email', error='Слишком много попыток. Повторите через 15 минут.'), 429
            with storage.SessionLocal() as db:
                account = db.scalar(select(WebAccount).where(WebAccount.email == request.form.get('email', '').strip().lower()))
                if not account or not check_password_hash(account.password_hash, request.form.get('password', '')):
                    return render('doctor_account', 'Вход врача по email', error='Неверный email или пароль существующего аккаунта.'), 400
                binding = db.get(DoctorWebAccount, account.user_id)
                if not binding:
                    db.add(DoctorWebAccount(user_id=account.user_id, telegram_id=identity()))
                else:
                    binding.telegram_id = identity()
                db.commit()
            flash('Аккаунт подтверждён. В приложении врача теперь можно войти с этим email и паролем.')
            return redirect('/doctor', code=303)
        return render('doctor_account', 'Вход врача по email')

    @app.get('/doctor/access/<token>')
    def doctor_access_preview(token):
        # GET does not consume links, so Telegram previews cannot log a doctor out.
        target=doctor_destination(request.args.get('next',session.get('doctor_next')))
        if identity():
            with storage.SessionLocal() as db:
                linked = db.scalar(select(DoctorWebAccount.user_id).where(DoctorWebAccount.telegram_id == identity()))
            return redirect(target if linked else '/doctor/account')
        session['doctor_next']=target
        return render('doctor_access','Вход для врача',access_token=token,next_target=target)

    @app.post('/doctor/access')
    def doctor_access_accept():
        target=doctor_destination(request.form.get('next') or session.get('doctor_next'))
        if not login_csrf_valid():
            # Do not consume a one-time link until a fresh, cookie-bound form is submitted.
            return render('doctor_access', 'Вход для врача',
                access_token=request.form.get('access_token', ''), next_target=target,
                error='Сеанс изменился. Форма обновлена — нажмите кнопку входа ещё раз.'), 400
        session['doctor_next']=target
        remember=request.form.get('remember')=='1'
        grant=consume_doctor_link(request.form.get('access_token',''),remember=remember)
        if not grant:
            return render('doctor_login','Вход для врача',error='Ссылка истекла или уже использована. Нажмите «Войти через Telegram», чтобы получить новую.'),400
        session['doctor_grant']=grant;session['doctor_csrf']=secrets.token_urlsafe(32)
        session.pop('doctor_next',None)
        if request.form.get('setup_email') == '1':
            actor = doctor_identity(grant)
            with storage.SessionLocal() as db:
                linked = db.scalar(select(DoctorWebAccount.user_id).where(DoctorWebAccount.telegram_id == actor)) if actor else None
            if not linked:
                target = '/doctor/account'
        response=redirect(target,code=303)
        response.set_cookie(DOCTOR_COOKIE,grant,max_age=REMEMBER_SECONDS if remember else None,secure=True,httponly=True,samesite='Lax',path='/')
        response.headers['Cache-Control']='private, no-store'
        response.headers['Referrer-Policy']='no-referrer'
        return response

    @app.post('/doctor/logout')
    @require_doctor
    def doctor_logout():
        check_csrf()
        from web_push import revoke_browser_subscriptions
        revoke_browser_subscriptions('doctor')
        with storage.SessionLocal() as db:
            grants={value for value in (request.cookies.get(DOCTOR_COOKIE),session.get('doctor_grant')) if value}
            db.execute(update(DoctorAccess).where(DoctorAccess.session_digest.in_([hashed(value) for value in grants])).values(session_expires_at=datetime.utcnow()));db.commit()
        session.pop('doctor_grant',None);session.pop('doctor_csrf',None)
        response=redirect(url_for('doctor_login'),code=303)
        response.delete_cookie(DOCTOR_COOKIE,secure=True,httponly=True,samesite='Lax',path='/')
        return response

    @app.get('/doctor/ai')
    @require_doctor
    def doctor_ai():
        now=datetime.utcnow()
        month_start=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        day_start=now.replace(hour=0,minute=0,second=0,microsecond=0)
        def usage_since(db,start):
            row=db.execute(select(
                func.count(billing.CreditUsage.id),
                func.coalesce(func.sum(billing.CreditUsage.input_tokens),0),
                func.coalesce(func.sum(billing.CreditUsage.output_tokens),0),
                func.coalesce(func.sum(billing.CreditUsage.credits),0),
            ).where(billing.CreditUsage.status=='success',billing.CreditUsage.created_at>=start)).one()
            return {'requests':int(row[0] or 0),'input_tokens':int(row[1] or 0),'output_tokens':int(row[2] or 0),'credits':int(row[3] or 0)}
        with storage.SessionLocal() as db:
            today=usage_since(db,day_start);month=usage_since(db,month_start)
            grants=db.scalars(select(billing.CreditGrant).where(billing.CreditGrant.origin.like(f'monthly:%:{now:%Y-%m}'))).all()
            free_issued=sum(x.total for x in grants);free_left=sum(x.remaining for x in grants);free_used=max(0,free_issued-free_left)
            paid_active=db.scalar(select(func.count(billing.CreditGrant.id)).where(
                billing.CreditGrant.origin.like('payment:%'),billing.CreditGrant.remaining>0,
                (billing.CreditGrant.expires_at.is_(None)) | (billing.CreditGrant.expires_at>now))) or 0
        input_rate=float(os.getenv('OPENAI_GPT56_SOL_INPUT_USD_PER_1M','4'))
        output_rate=float(os.getenv('OPENAI_GPT56_SOL_OUTPUT_USD_PER_1M','20'))
        estimated=(month['input_tokens']*input_rate+month['output_tokens']*output_rate)/1_000_000
        try:budget=max(0.0,float(os.getenv('MYDOCTOR_AI_BUDGET_USD','100')))
        except ValueError:budget=100.0
        budget_pct=(estimated/budget*100) if budget else 0
        budget_state='danger' if budget and budget_pct>=95 else 'warning' if budget and budget_pct>=80 else 'ok'
        return render('doctor_ai','AI расходы',today=today,month=month,free_issued=free_issued,free_used=free_used,
            free_left=free_left,paid_active=int(paid_active),estimated_cost=estimated,budget=budget,budget_pct=budget_pct,
            budget_state=budget_state,input_rate=input_rate,output_rate=output_rate)

    @app.get('/doctor/dashboard')
    @app.get('/doctor/')
    @app.get('/doctor')
    @require_doctor
    def doctor_dashboard():
        status=request.args.get('status','');q=request.args.get('q','').strip()[:200]
        if status and status not in STATUSES:abort(400)
        page=max(1,request.args.get('page',1,type=int));limit=30
        with storage.SessionLocal() as db:
            current=func.coalesce(ConsultationCase.status,'new')
            base=select(storage.Consultation,ConsultationCase,storage.User).join(storage.User).outerjoin(ConsultationCase,ConsultationCase.consultation_id==storage.Consultation.id).where(storage.Consultation.kind=='consult_request')
            counts=dict(db.execute(select(current,func.count(storage.Consultation.id)).select_from(storage.Consultation).outerjoin(ConsultationCase,ConsultationCase.consultation_id==storage.Consultation.id).where(storage.Consultation.kind=='consult_request').group_by(current)).all())
            if status:base=base.where(current==status)
            if q:
                condition=func.lower(storage.Consultation.user_text).contains(q.lower(),autoescape=True)
                if q.isdigit():condition=or_(condition,storage.Consultation.id==int(q))
                base=base.where(condition)
            activity=select(ConsultationMessage.consultation_id,func.max(ConsultationMessage.created_at).label('last_at')).group_by(ConsultationMessage.consultation_id).subquery()
            base=base.outerjoin(activity,activity.c.consultation_id==storage.Consultation.id)
            rows=db.execute(base.order_by(func.coalesce(activity.c.last_at,storage.Consultation.created_at).desc(),storage.Consultation.id.desc()).offset((page-1)*limit).limit(limit+1)).all()
            unread=unread_counts(db,'doctor',ids=[row[0].id for row in rows])
            return render('doctor_dashboard','Кабинет врача',rows=rows[:limit],has_more=len(rows)>limit,page_number=page,counts=counts,status=status,q=q,unread=unread)

    @app.route('/doctor/requests/<int:rid>',methods=['GET','POST'])
    @require_doctor
    def doctor_request(rid):
        with storage.SessionLocal() as db:
            query=select(storage.Consultation).where(storage.Consultation.id==rid,storage.Consultation.kind=='consult_request')
            record=db.scalar(query.with_for_update() if request.method=='POST' else query)
            if not record:abort(404)
            case=db.get(ConsultationCase,rid)
            from web_app import WebAccount
            chat_available=bool(db.scalar(select(WebAccount.id).where(WebAccount.user_id==record.user_id)))
            chat=thread_context(db,rid,'doctor') if chat_available else {}
            if request.method=='POST':
                check_csrf()
                status=request.form.get('status','');paid=request.form.get('paid_confirmed')=='1'
                notes=request.form.get('doctor_notes','').strip()
                error=None;code=400
                if status not in STATUSES:error='Выберите статус заявки.'
                elif status in ('confirmed','in_progress','completed') and not paid:error='Сначала подтвердите получение оплаты.'
                elif len(notes)>10000:error='Заметка должна быть не длиннее 10000 символов.'
                elif request.form.get('version',type=int)!=(case.version if case else 0):error='Заявка уже изменена. Обновите страницу перед сохранением.';code=409
                if error:
                    return render('doctor_request',t('Заявка №')+str(rid),record=record,case=case,files=request_files(db,rid),contact=db.get(ConsultationContact,rid),events=db.scalars(select(RequestStatusEvent).where(RequestStatusEvent.consultation_id==rid).order_by(RequestStatusEvent.id.desc())).all(),error=error,chat_available=chat_available,**chat),code
                if not case:case=ConsultationCase(consultation_id=rid,consultation_type='unknown',patient={},contacts={},status='new',version=0);db.add(case)
                db.add(RequestStatusEvent(consultation_id=rid,actor_telegram_id=identity(),previous_status=case.status,status=status,paid_confirmed=paid))
                case.status=status;case.paid_confirmed=paid;case.doctor_notes=notes;case.version+=1;case.updated_at=datetime.utcnow();db.commit()
                flash('Изменения сохранены. Статус доступен владельцу в его заявке.')
                return redirect(url_for('doctor_request',rid=rid),code=303)
            return render('doctor_request',t('Заявка №')+str(rid),record=record,case=case,files=request_files(db,rid),contact=db.get(ConsultationContact,rid),events=db.scalars(select(RequestStatusEvent).where(RequestStatusEvent.consultation_id==rid).order_by(RequestStatusEvent.id.desc())).all(),chat_available=chat_available,**chat)

    @app.get('/doctor/clients')
    @require_doctor
    def doctor_clients():
        q=request.args.get('q','').strip()[:200];page=max(1,request.args.get('page',1,type=int));limit=30
        with storage.SessionLocal() as db:
            query=select(storage.User.id,storage.User.first_name,func.count(storage.Consultation.id).label('total'),
                func.max(storage.Consultation.created_at).label('last_at')).join(storage.Consultation,
                storage.Consultation.user_id==storage.User.id).where(storage.Consultation.kind=='consult_request')
            if q:query=query.where(or_(func.lower(storage.User.first_name).contains(q.lower(),autoescape=True),
                func.lower(storage.Consultation.user_text).contains(q.lower(),autoescape=True)))
            rows=db.execute(query.group_by(storage.User.id,storage.User.first_name).order_by(func.max(storage.Consultation.created_at).desc())
                .offset((page-1)*limit).limit(limit+1)).all()
            return render('doctor_clients','Картотека владельцев',rows=rows[:limit],has_more=len(rows)>limit,page_number=page,q=q)

    @app.get('/doctor/clients/<int:uid>')
    @require_doctor
    def doctor_client(uid):
        page=max(1,request.args.get('page',1,type=int));limit=30
        with storage.SessionLocal() as db:
            owner=db.get(storage.User,uid)
            if not owner:abort(404)
            query=select(storage.Consultation,ConsultationCase).outerjoin(ConsultationCase,
                ConsultationCase.consultation_id==storage.Consultation.id).where(storage.Consultation.user_id==uid,storage.Consultation.kind=='consult_request')
            latest=db.execute(query.order_by(storage.Consultation.id.desc()).limit(1)).first()
            if not latest:abort(404)
            rows=db.execute(query.order_by(storage.Consultation.id.desc()).offset((page-1)*limit).limit(limit+1)).all()
            ids=[row[0].id for row in rows[:limit]]
            documents=[dict(filename=file.filename,url=url_for('consultation_file',fid=file.id),rid=file.consultation_id)
                for file in db.scalars(select(RequestAttachment).where(RequestAttachment.consultation_id.in_(ids))).all()]
            for file,rid in db.execute(select(MessageFile,ConsultationMessage.consultation_id).join(ConsultationMessage,
                    MessageFile.message_id==ConsultationMessage.id).where(ConsultationMessage.consultation_id.in_(ids))):
                documents.append(dict(filename=file.filename,url=url_for('message_file',fid=file.id),rid=rid))
            return render('doctor_client','Карточка владельца',owner=owner,latest_case=latest[1],rows=rows[:limit],
                documents=documents,unread=unread_counts(db,'doctor',ids=ids),page_number=page,has_more=len(rows)>limit)

    @app.get('/consultation/files/<int:fid>')
    def consultation_file(fid):
        if not session.get('uid') and not identity():abort(401)
        with storage.SessionLocal() as db:
            file=db.get(RequestAttachment,fid)
            record=db.get(storage.Consultation,file.consultation_id) if file else None
            if not record or (record.user_id!=session.get('uid') and not identity()):abort(404)
            source=attachment_content(db,file,record.user_id,WebDocument)
            if not source:abort(404)
            response=send_file(io.BytesIO(source.data),mimetype=source.mime_type,download_name=file.filename,as_attachment=True)
            response.headers['Cache-Control']='private, no-store'
            return response
