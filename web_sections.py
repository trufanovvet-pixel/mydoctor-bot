import io, math, secrets
from datetime import date, datetime
from functools import wraps
from flask import request, session, redirect, url_for, render_template, abort, flash, send_file, jsonify
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from PIL import Image, ImageOps, UnidentifiedImageError
import storage
from patient_records import CATEGORIES, DocumentLabel, PetPhoto, OperationRecord, OperationAttachment, classify_document
from prevention_patch import PreventiveEvent
from consultation_delivery import WebConsultationDelivery
from owner_profile import OwnerProfile, ConsultationContact, profile_values, validate_contacts, preferred_contact, contact_links
from web_i18n import t, language

MAX_FILE=20*1024*1024
KINDS={'vaccination':'Вакцинация','worms':'Обработка от гельминтов','ecto':'Блохи и клещи','checkup':'Контрольный осмотр'}

def verified_file(file,photo=False):
    if not file or not file.filename:raise ValueError('Выберите файл.')
    data=file.read(MAX_FILE+1)
    if not data or len(data)>MAX_FILE:raise ValueError('Максимальный размер файла — 20 МБ.')
    if not photo and data.startswith(b'%PDF-'):return data,'application/pdf'
    try:
        image=Image.open(io.BytesIO(data))
        if image.width*image.height>25_000_000:raise ValueError('Изображение слишком большое. Пришлите уменьшенную копию.')
        image.load(); image=ImageOps.exif_transpose(image).convert('RGB');image.thumbnail((1200,1200) if photo else (3000,3000))
        output=io.BytesIO();image.save(output,format='JPEG',quality=88)
        return output.getvalue(),'image/jpeg'
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError):raise ValueError('Нужен файл изображения или PDF.' if not photo else 'Нужно фото в формате JPEG, PNG или WebP.')

def install(app,WebDocument):
    storage.Base.metadata.create_all(storage.engine)
    def login_required(fn):
        @wraps(fn)
        def wrapped(*a,**kw):
            if not session.get('uid'):return redirect(url_for('login',next=request.path))
            return fn(*a,**kw)
        return wrapped
    def owned_pet(db,pid):
        pet=db.scalar(select(storage.Pet).where(storage.Pet.id==pid,storage.Pet.user_id==session['uid']))
        if not pet:abort(404)
        return pet
    def owned_op(db,oid):
        op=db.scalar(select(OperationRecord).where(OperationRecord.id==oid,OperationRecord.user_id==session['uid']))
        if not op:abort(404)
        return op
    def pets_for(db):return db.scalars(select(storage.Pet).where(storage.Pet.user_id==session['uid']).order_by(storage.Pet.created_at)).all()
    def render(page,title,**kw):return render_template('section.html',page=page,title=title if page in ('pet','analysis','operation') else t(title),categories=CATEGORIES,kinds=KINDS,**kw)
    def picked_pet(db):
        pid=request.args.get('pet_id',type=int)
        if pid:owned_pet(db,pid)
        return pid

    @app.get('/dashboard')
    @login_required
    def dashboard():return render('dashboard','МойДоктор')

    @app.get('/how-it-works')
    def how_it_works():return render('how','Как это работает')

    @app.route('/profile',methods=['GET','POST'])
    @login_required
    def owner_profile_page():
        with storage.SessionLocal() as db:
            profile=db.get(OwnerProfile,session['uid'])
            user=db.get(storage.User,session['uid'])
            from web_app import WebAccount
            account=db.scalar(select(WebAccount).where(WebAccount.user_id==session['uid']))
            values=profile_values(profile,user,account.email if account else '')
            session.setdefault('profile_key',secrets.token_urlsafe(32))
            if request.method=='POST':
                try:
                    if not secrets.compare_digest(request.form.get('profile_key',''),session['profile_key']):
                        raise ValueError('Форма устарела. Обновите страницу и попробуйте ещё раз.')
                    values=validate_contacts(request.form)
                    if not profile:
                        profile=OwnerProfile(user_id=session['uid']);db.add(profile)
                    for key,value in values.items():setattr(profile,key,value)
                    profile.language=language()
                    db.commit();flash('Контакты сохранены.')
                    return redirect(url_for('owner_profile_page'),code=303)
                except ValueError as exc:
                    return render('profile','Мой профиль',values=request.form,error=str(exc),profile_key=session['profile_key']),400
            return render('profile','Мой профиль',values=values,profile_key=session['profile_key'])

    @app.get('/pets')
    @login_required
    def pets_page():
        with storage.SessionLocal() as db:
            pets=pets_for(db);photos=set(db.scalars(select(PetPhoto.pet_id).where(PetPhoto.user_id==session['uid'])).all())
            return render('pets','Мои питомцы',pets=pets,photos=photos)

    @app.route('/pets/<int:pid>',methods=['GET','POST'])
    @login_required
    def pet_page(pid):
        with storage.SessionLocal() as db:
            pet=owned_pet(db,pid)
            if request.method=='POST':
                try:
                    name=request.form.get('name','').strip();species=request.form.get('species','').strip()
                    if not name or species not in ('Собака','Кошка','собака','кошка'):raise ValueError('Укажите имя и вид питомца.')
                    weight=float(request.form['weight'].replace(',','.')) if request.form.get('weight') else None
                    if weight is not None and (not math.isfinite(weight) or not 0<weight<=300):raise ValueError('Проверьте вес питомца.')
                    pet.name=name[:120];pet.species=species;pet.weight_kg=weight
                    for field,limit in [('breed',120),('age',80),('sex',40)]:setattr(pet,field,request.form.get(field,'').strip()[:limit] or None)
                    db.commit();flash('Анкета сохранена.')
                except ValueError as exc:flash(str(exc))
                return redirect(url_for('pet_page',pid=pid))
            photo=db.get(PetPhoto,pid)
            return render('pet',pet.name,pet=pet,photo=photo)

    @app.route('/pets/<int:pid>/photo',methods=['GET','POST'])
    @login_required
    def pet_photo(pid):
        with storage.SessionLocal() as db:
            owned_pet(db,pid);photo=db.get(PetPhoto,pid)
            if request.method=='POST':
                try:
                    data,mime=verified_file(request.files.get('photo'),photo=True)
                    if not photo:photo=PetPhoto(pet_id=pid,user_id=session['uid'],data=data,mime_type=mime);db.add(photo)
                    else:photo.data=data;photo.mime_type=mime
                    photo.updated_at=datetime.utcnow();db.commit();flash('Фото питомца сохранено.')
                except ValueError as exc:flash(str(exc))
                return redirect(url_for('pet_page',pid=pid))
            if not photo or photo.user_id!=session['uid']:abort(404)
            r=send_file(io.BytesIO(photo.data),mimetype=photo.mime_type);r.headers['Cache-Control']='private, no-store';return r

    @app.get('/analyses')
    @login_required
    def analyses_page():
        with storage.SessionLocal() as db:
            pid=picked_pet(db);category=request.args.get('category','')
            if category and category not in CATEGORIES:abort(404)
            query=select(WebDocument).where(WebDocument.user_id==session['uid'])
            if pid:query=query.where(WebDocument.pet_id==pid)
            docs=db.scalars(query.order_by(WebDocument.created_at.desc())).all()
            labels={x.document_id:x for x in db.scalars(select(DocumentLabel).where(DocumentLabel.source=='web')).all()}
            items=[];counts={key:0 for key in CATEGORIES}
            for d in docs:
                label=labels.get(d.id);kind=label.category if label else classify_document(d.filename+"\n"+(d.analysis or "")[:1500])
                if kind not in CATEGORIES:kind='other'
                counts[kind]+=1
                if not category or kind==category:items.append({'id':d.id,'filename':d.filename,'date':label.study_date if label and label.study_date else d.created_at,'category':kind,'pet_id':d.pet_id})
            return render('analyses','Анализы и исследования',items=items,counts=counts,pets=pets_for(db),pet_id=pid,category=category)

    @app.route('/analyses/<int:did>',methods=['GET','POST'])
    @login_required
    def analysis_page(did):
        with storage.SessionLocal() as db:
            doc=db.scalar(select(WebDocument).where(WebDocument.id==did,WebDocument.user_id==session['uid']))
            if not doc:abort(404)
            label=db.get(DocumentLabel,('web',did))
            if request.method=='POST':
                cat=request.form.get('category','other')
                if cat not in CATEGORIES:abort(400)
                pid=request.form.get('pet_id',type=int)
                if pid:owned_pet(db,pid)
                try:study_date=date.fromisoformat(request.form['study_date']) if request.form.get('study_date') else None
                except ValueError:flash('Проверьте дату.');return redirect(url_for('analysis_page',did=did))
                doc.pet_id=pid
                if not label:label=DocumentLabel(source='web',document_id=did);db.add(label)
                label.category=cat;label.study_date=study_date;db.commit();flash('Данные исследования сохранены.')
                return redirect(url_for('analysis_page',did=did))
            return render('analysis',doc.filename,doc=doc,label=label,pets=pets_for(db),category=label.category if label else classify_document(doc.filename+"\n"+(doc.analysis or "")[:1500]))

    @app.get('/assistant')
    @login_required
    def assistant_page():
        with storage.SessionLocal() as db:
            query=select(storage.Consultation).where(storage.Consultation.user_id==session['uid'],storage.Consultation.kind=='web_chat')
            if session.get('chat_started_at'):
                try:query=query.where(storage.Consultation.created_at>=datetime.fromisoformat(session['chat_started_at']))
                except ValueError:pass
            chats=db.scalars(query.order_by(storage.Consultation.id.desc()).limit(40)).all()
            return render('assistant','Медицинский помощник',chats=list(reversed(chats)))

    @app.get('/history')
    @login_required
    def history_page():
        with storage.SessionLocal() as db:
            pid=picked_pet(db);query=select(storage.Consultation).where(storage.Consultation.user_id==session['uid'])
            if pid:query=query.where(storage.Consultation.pet_id==pid)
            rows=db.scalars(query.order_by(storage.Consultation.id.desc()).limit(100)).all()
            return render('history','История обращений',rows=rows)

    @app.route('/operations',methods=['GET','POST'])
    @login_required
    def operations_page():
        with storage.SessionLocal() as db:
            if request.method=='POST':
                pid=request.form.get('pet_id',type=int);owned_pet(db,pid)
                title=request.form.get('title','').strip()
                if not title:flash('Укажите название операции.');return redirect(url_for('operations_page'))
                try:op_date=date.fromisoformat(request.form['operation_date']) if request.form.get('operation_date') else None
                except ValueError:flash('Проверьте дату операции.');return redirect(url_for('operations_page'))
                op=OperationRecord(user_id=session['uid'],pet_id=pid,title=title[:255],operation_date=op_date,notes=request.form.get('notes','').strip()[:10000]);db.add(op);db.commit()
                return redirect(url_for('operation_page',oid=op.id))
            pid=picked_pet(db);query=select(OperationRecord).where(OperationRecord.user_id==session['uid'])
            if pid:query=query.where(OperationRecord.pet_id==pid)
            return render('operations','Операции',operations=db.scalars(query.order_by(OperationRecord.created_at.desc())).all(),pets=pets_for(db),pet_id=pid)

    @app.route('/operations/<int:oid>',methods=['GET','POST'])
    @login_required
    def operation_page(oid):
        with storage.SessionLocal() as db:
            op=owned_op(db,oid)
            if request.method=='POST':
                try:
                    file=request.files.get('file');data,mime=verified_file(file)
                    name=(file.filename or 'Выписка').replace('\\','/').split('/')[-1][:255]
                    db.add(OperationAttachment(operation_id=oid,user_id=session['uid'],filename=name,data=data,mime_type=mime));db.commit();flash('Выписка сохранена.')
                except ValueError as exc:flash(str(exc))
                return redirect(url_for('operation_page',oid=oid))
            files=db.scalars(select(OperationAttachment).where(OperationAttachment.operation_id==oid,OperationAttachment.user_id==session['uid'])).all()
            return render('operation',op.title,op=op,pet=owned_pet(db,op.pet_id),files=files)

    @app.get('/operation-files/<int:fid>')
    @login_required
    def operation_file(fid):
        with storage.SessionLocal() as db:
            file=db.scalar(select(OperationAttachment).where(OperationAttachment.id==fid,OperationAttachment.user_id==session['uid']))
            if not file or not file.data:abort(404)
            owned_op(db,file.operation_id)
            return send_file(io.BytesIO(file.data),mimetype=file.mime_type,download_name=file.filename,as_attachment=True)

    @app.route('/prevention',methods=['GET','POST'])
    @login_required
    def prevention_page():
        with storage.SessionLocal() as db:
            if request.method=='POST':
                pid=request.form.get('pet_id',type=int);owned_pet(db,pid);kind=request.form.get('kind')
                if kind not in KINDS:abort(400)
                try:due=date.fromisoformat(request.form.get('due_date',''))
                except ValueError:flash('Укажите дату.');return redirect(url_for('prevention_page'))
                db.add(PreventiveEvent(user_id=session['uid'],pet_id=pid,kind=kind,due_date=due,note=request.form.get('note','')[:255]));db.commit();flash('Событие добавлено в календарь.')
                return redirect(url_for('prevention_page'))
            rows=db.scalars(select(PreventiveEvent).where(PreventiveEvent.user_id==session['uid']).order_by(PreventiveEvent.due_date)).all()
            return render('prevention','Профилактика',events=rows,pets=pets_for(db),today=date.today())

    @app.route('/consultation',methods=['GET','POST'])
    @login_required
    def consultation_page():
        with storage.SessionLocal() as db:
            profile=db.get(OwnerProfile,session['uid'])
            contacts=profile_values(profile) if profile else None
            if request.method=='POST':
                key=request.form.get('request_key','')
                existing=db.scalar(select(WebConsultationDelivery).join(storage.Consultation).where(
                    WebConsultationDelivery.request_key==key,storage.Consultation.user_id==session['uid']))
                if existing:return redirect(url_for('consultation_request_page',rid=existing.consultation_id),code=303)
                contact=request.form.get('contact','').strip();question=request.form.get('question','').strip()
                error=None
                if not contact or not question:error='Укажите контакт и причину обращения.'
                elif len(contact)>200 or len(question)>6000:error='Контакт — до 200 символов, причина обращения — до 6000.'
                elif not key or not secrets.compare_digest(key,session.get('consultation_key','')):error='Форма устарела. Проверьте данные и нажмите «Отправить заявку» ещё раз.'
                if error:
                    session.setdefault('consultation_key',secrets.token_urlsafe(32))
                    return render('consultation','Консультация врача',requests=[],values=request.form,contacts=contacts,error=error,request_key=session['consultation_key']),400
                text=t('Контакт')+': '+contact[:200]+'\n'+t('Формат')+': '+t(request.form.get('format','')[:60])+'\n'+t('Запрос')+': '+question[:6000]
                if contacts:
                    labels={'name':'Владелец','phone':'Телефон','email':'Почта','telegram':'Telegram','whatsapp':'WhatsApp','instagram':'Instagram','vk':'ВКонтакте'}
                    lines=[t(label)+': '+contacts[key] for key,label in labels.items() if contacts.get(key)]
                    text='\n'.join(lines)+'\n'+t('Предпочтительный способ связи')+': '+contacts['preferred']+'\n\n'+text
                text=t('Язык общения')+': '+('English' if language()=='en' else 'Русский')+'\n'+text
                record=storage.Consultation(user_id=session['uid'],pet_id=None,kind='consult_request',user_text=text,assistant_text='Заявка сохранена и ожидает отправки врачу.')
                try:
                    db.add(record);db.flush()
                    db.add(ConsultationContact(consultation_id=record.id,links=contact_links(contacts or {}),language=language()))
                    db.add(WebConsultationDelivery(consultation_id=record.id,request_key=key));db.commit()
                    rid=record.id
                except IntegrityError:
                    db.rollback()
                    existing=db.scalar(select(WebConsultationDelivery).join(storage.Consultation).where(WebConsultationDelivery.request_key==key,storage.Consultation.user_id==session['uid']))
                    if not existing:raise
                    rid=existing.consultation_id
                return redirect(url_for('consultation_request_page',rid=rid),code=303)
            session['consultation_key']=secrets.token_urlsafe(32)
            rows=db.scalars(select(storage.Consultation).where(storage.Consultation.user_id==session['uid'],storage.Consultation.kind=='consult_request').order_by(storage.Consultation.id.desc()).limit(10)).all()
            return render('consultation','Консультация врача',requests=rows,values={'contact':preferred_contact(contacts) if contacts else ''},contacts=contacts,request_key=session['consultation_key'])

    @app.get('/consultation/requests/<int:rid>')
    @login_required
    def consultation_request_page(rid):
        with storage.SessionLocal() as db:
            row=db.scalar(select(storage.Consultation).where(storage.Consultation.id==rid,storage.Consultation.user_id==session['uid'],storage.Consultation.kind=='consult_request'))
            if not row:abort(404)
            delivery=db.scalar(select(WebConsultationDelivery).where(WebConsultationDelivery.consultation_id==rid))
            state=delivery.state if delivery else 'saved'
            if request.args.get('status')=='1':
                response=jsonify(state=state,message=t(row.assistant_text))
                response.headers['Cache-Control']='private, no-store'
                return response
            return render('consultation_request',t('Заявка №')+str(row.id),record=row,state=state)
