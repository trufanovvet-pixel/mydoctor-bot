import io, math
from datetime import date, datetime
from functools import wraps
from flask import request, session, redirect, url_for, render_template, abort, flash, send_file
from sqlalchemy import select
from PIL import Image, ImageOps, UnidentifiedImageError
import storage
from patient_records import CATEGORIES, DocumentLabel, PetPhoto, OperationRecord, OperationAttachment, classify_document
from prevention_patch import PreventiveEvent

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
    def render(page,title,**kw):return render_template('section.html',page=page,title=title,categories=CATEGORIES,kinds=KINDS,**kw)
    def picked_pet(db):
        pid=request.args.get('pet_id',type=int)
        if pid:owned_pet(db,pid)
        return pid

    @app.get('/dashboard')
    @login_required
    def dashboard():return render('dashboard','МойДоктор')

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
            if request.method=='POST':
                contact=request.form.get('contact','').strip();question=request.form.get('question','').strip()
                if not contact or not question:flash('Укажите контакт и причину обращения.');return redirect(url_for('consultation_page'))
                text='Контакт: '+contact[:200]+'\nФормат: '+request.form.get('format','')[:60]+'\nЗапрос: '+question[:6000]
                db.add(storage.Consultation(user_id=session['uid'],pet_id=None,kind='consult_request',user_text=text,assistant_text='Заявка сохранена. Время и оплата ещё не подтверждены.'));db.commit()
                flash('Заявка сохранена в кабинете. Это ещё не подтверждённая запись: время и оплату нужно согласовать с врачом.')
                return redirect(url_for('consultation_page'))
            rows=db.scalars(select(storage.Consultation).where(storage.Consultation.user_id==session['uid'],storage.Consultation.kind=='consult_request').order_by(storage.Consultation.id.desc()).limit(10)).all()
            return render('consultation','Консультация врача',requests=rows)
