import io
import secrets
from datetime import datetime
from functools import wraps
from flask import g,request,session,redirect,url_for,render_template,abort,flash,send_file
from sqlalchemy import select,func,or_,update
import storage
from consultation_cases import ConsultationCase,RequestStatusEvent,RequestAttachment,STATUSES,TYPES,request_files,attachment_content
from owner_profile import ConsultationContact
from doctor_access import DoctorAccess,consume_doctor_link,doctor_identity,hashed
from web_i18n import t


def install(app,WebDocument):
    storage.Base.metadata.create_all(storage.engine)

    def identity():
        if not hasattr(g,'doctor_identity'):g.doctor_identity=doctor_identity(session.get('doctor_grant'))
        return g.doctor_identity

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

    def render(page,title,**kw):
        response=app.make_response(render_template('section.html',page=page,title=t(title),statuses=STATUSES,consultation_types=TYPES,doctor_csrf=csrf(),**kw))
        response.headers['Cache-Control']='private, no-store'
        response.headers['Referrer-Policy']='no-referrer'
        return response

    @app.get('/doctor/login')
    def doctor_login():
        if identity():return redirect(url_for('doctor_dashboard'))
        return render('doctor_login','Вход для врача')

    @app.get('/doctor/access/<token>')
    def doctor_access_preview(token):
        # GET does not consume links, so Telegram previews cannot log a doctor out.
        return render('doctor_access','Вход для врача',access_token=token)

    @app.post('/doctor/access')
    def doctor_access_accept():
        check_csrf()
        grant=consume_doctor_link(request.form.get('access_token',''))
        if not grant:
            return render('doctor_login','Вход для врача',error='Ссылка истекла или уже использована. Получите новую командой /doctor в боте.'),400
        session['doctor_grant']=grant;session['doctor_csrf']=secrets.token_urlsafe(32)
        target=session.pop('doctor_next','/doctor')
        if not target.startswith('/doctor') or '\\' in target:target='/doctor'
        return redirect(target,code=303)

    @app.post('/doctor/logout')
    @require_doctor
    def doctor_logout():
        check_csrf()
        with storage.SessionLocal() as db:
            db.execute(update(DoctorAccess).where(DoctorAccess.session_digest==hashed(session['doctor_grant'])).values(session_expires_at=datetime.utcnow()));db.commit()
        session.pop('doctor_grant',None);session.pop('doctor_csrf',None)
        return redirect(url_for('doctor_login'),code=303)

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
            rows=db.execute(base.order_by(storage.Consultation.id.desc()).offset((page-1)*limit).limit(limit+1)).all()
            return render('doctor_dashboard','Кабинет врача',rows=rows[:limit],has_more=len(rows)>limit,page_number=page,counts=counts,status=status,q=q)

    @app.route('/doctor/requests/<int:rid>',methods=['GET','POST'])
    @require_doctor
    def doctor_request(rid):
        with storage.SessionLocal() as db:
            query=select(storage.Consultation).where(storage.Consultation.id==rid,storage.Consultation.kind=='consult_request')
            record=db.scalar(query.with_for_update() if request.method=='POST' else query)
            if not record:abort(404)
            case=db.get(ConsultationCase,rid)
            if request.method=='POST':
                check_csrf()
                status=request.form.get('status','');paid=request.form.get('paid_confirmed')=='1'
                notes=request.form.get('doctor_notes','').strip()
                error=None;code=400
                if status not in STATUSES:error='Выберите статус заявки.'
                elif status in ('confirmed','completed') and not paid:error='Сначала подтвердите получение оплаты.'
                elif len(notes)>10000:error='Заметка должна быть не длиннее 10000 символов.'
                elif request.form.get('version',type=int)!=(case.version if case else 0):error='Заявка уже изменена. Обновите страницу перед сохранением.';code=409
                if error:
                    return render('doctor_request',t('Заявка №')+str(rid),record=record,case=case,files=request_files(db,rid),contact=db.get(ConsultationContact,rid),events=db.scalars(select(RequestStatusEvent).where(RequestStatusEvent.consultation_id==rid).order_by(RequestStatusEvent.id.desc())).all(),error=error),code
                if not case:case=ConsultationCase(consultation_id=rid,consultation_type='unknown',patient={},contacts={},status='new',version=0);db.add(case)
                db.add(RequestStatusEvent(consultation_id=rid,actor_telegram_id=identity(),previous_status=case.status,status=status,paid_confirmed=paid))
                case.status=status;case.paid_confirmed=paid;case.doctor_notes=notes;case.version+=1;case.updated_at=datetime.utcnow();db.commit()
                flash('Изменения сохранены. Статус доступен владельцу в его заявке.')
                return redirect(url_for('doctor_request',rid=rid),code=303)
            return render('doctor_request',t('Заявка №')+str(rid),record=record,case=case,files=request_files(db,rid),contact=db.get(ConsultationContact,rid),events=db.scalars(select(RequestStatusEvent).where(RequestStatusEvent.consultation_id==rid).order_by(RequestStatusEvent.id.desc())).all())

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
