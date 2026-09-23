"""Consultation intake and workflow metadata shared by the website and bot."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, select, func
from sqlalchemy.orm import Mapped, mapped_column
import storage
from patient_records import OperationAttachment, OperationRecord

TYPES={'primary':'Первичная консультация','followup':'Повторная консультация'}
STATUSES={'new':'Новая','contacting':'Согласовываем время','awaiting_payment':'Ожидает оплаты',
          'confirmed':'Запись подтверждена','completed':'Завершена','cancelled':'Отменена'}


class ConsultationCase(storage.Base):
    __tablename__='consultation_cases'
    consultation_id: Mapped[int]=mapped_column(ForeignKey('consultations.id'),primary_key=True)
    consultation_type: Mapped[str]=mapped_column(String(20),default='primary')
    patient: Mapped[dict]=mapped_column(JSON,default=dict)
    contacts: Mapped[dict]=mapped_column(JSON,default=dict)
    status: Mapped[str]=mapped_column(String(30),default='new',index=True)
    paid_confirmed: Mapped[bool]=mapped_column(Boolean,default=False)
    doctor_notes: Mapped[str]=mapped_column(Text,default='')
    version: Mapped[int]=mapped_column(Integer,default=0)
    updated_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)


class RequestAttachment(storage.Base):
    __tablename__='consultation_attachments'
    id: Mapped[int]=mapped_column(Integer,primary_key=True)
    consultation_id: Mapped[int]=mapped_column(ForeignKey('consultations.id'),index=True)
    source: Mapped[str]=mapped_column(String(20))
    source_id: Mapped[int]=mapped_column(Integer)
    filename: Mapped[str]=mapped_column(String(255))


class RequestStatusEvent(storage.Base):
    __tablename__='consultation_status_events'
    id: Mapped[int]=mapped_column(Integer,primary_key=True)
    consultation_id: Mapped[int]=mapped_column(ForeignKey('consultations.id'),index=True)
    actor_telegram_id: Mapped[int]=mapped_column(BigInteger)
    previous_status: Mapped[str]=mapped_column(String(30))
    status: Mapped[str]=mapped_column(String(30))
    paid_confirmed: Mapped[bool]=mapped_column(Boolean)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)


def available_documents(db, uid, WebDocument):
    rows=[]
    for d in db.execute(select(WebDocument.id,WebDocument.filename,WebDocument.pet_id).where(WebDocument.user_id==uid).order_by(WebDocument.created_at.desc())):
        rows.append(dict(key=f'web:{d.id}',filename=d.filename,pet_id=d.pet_id))
    for fid,filename,pid in db.execute(select(OperationAttachment.id,OperationAttachment.filename,OperationRecord.pet_id).join(OperationRecord).where(
            OperationAttachment.user_id==uid,OperationRecord.user_id==uid,OperationAttachment.data.is_not(None))):
        rows.append(dict(key=f'operation:{fid}',filename=filename,pet_id=pid))
    return rows


def validate_intake(db, uid, form, WebDocument):
    kind=form.get('consultation_type','primary')
    if kind not in TYPES:raise ValueError('Выберите тип консультации.')
    raw_pid=form.get('pet_id','')
    try:pid=int(raw_pid) if raw_pid else None
    except ValueError:raise ValueError('Выберите питомца из списка.')
    if pid is not None and pid<=0:raise ValueError('Выберите питомца из списка.')
    patient={}
    if pid:
        pet=db.scalar(select(storage.Pet).where(storage.Pet.id==pid,storage.Pet.user_id==uid))
        if not pet:raise ValueError('Выберите питомца из списка.')
        patient={key:getattr(pet,key) for key in ('name','species','breed','age','sex','weight_kg')}
    else:
        summary=form.get('pet_summary','').strip()
        if not summary or len(summary)>1000:raise ValueError('Выберите питомца или кратко опишите его: имя, вид, возраст и вес.')
        patient={'summary':summary}
    symptoms_since=form.get('symptoms_since','').strip()
    treatment=form.get('treatment','').strip()
    if len(symptoms_since)>300 or len(treatment)>4000:raise ValueError('Сократите описание сроков до 300 символов, лечения — до 4000.')
    selected=list(dict.fromkeys(form.getlist('documents')))
    if len(selected)>10:raise ValueError('Можно приложить до 10 документов, общим размером до 40 МБ.')
    attachments=[];total=0
    for key in selected:
        try:source,value=key.split(':');sid=int(value)
        except ValueError:raise ValueError('Один из выбранных документов недоступен.')
        if source=='web':
            item=db.execute(select(WebDocument.id,WebDocument.filename,WebDocument.pet_id,func.length(WebDocument.data)).where(WebDocument.id==sid,WebDocument.user_id==uid)).first()
        elif source=='operation':
            item=db.execute(select(OperationAttachment.id,OperationAttachment.filename,OperationRecord.pet_id,func.length(OperationAttachment.data)).join(OperationRecord).where(
                OperationAttachment.id==sid,OperationAttachment.user_id==uid,OperationRecord.user_id==uid,OperationAttachment.data.is_not(None))).first()
        else:item=None
        if not item or (pid and item[2] not in (None,pid)):raise ValueError('Один из выбранных документов недоступен или относится к другому питомцу.')
        total+=item[3] or 0
        if total>40*1024*1024:raise ValueError('Можно приложить до 10 документов, общим размером до 40 МБ.')
        attachments.append(dict(source=source,source_id=sid,filename=item[1]))
    return dict(kind=kind,pet_id=pid,patient=patient,symptoms_since=symptoms_since,treatment=treatment,attachments=attachments)


def request_files(db,rid):
    return db.scalars(select(RequestAttachment).where(RequestAttachment.consultation_id==rid).order_by(RequestAttachment.id)).all()


def attachment_content(db, attachment, uid, WebDocument):
    if attachment.source=='web':
        row=db.scalar(select(WebDocument).where(WebDocument.id==attachment.source_id,WebDocument.user_id==uid))
    elif attachment.source=='operation':
        row=db.scalar(select(OperationAttachment).join(OperationRecord).where(OperationAttachment.id==attachment.source_id,
            OperationAttachment.user_id==uid,OperationRecord.user_id==uid))
    else:row=None
    return row if row and row.data else None
