"""Shared patient record types; additive tables preserve all existing records."""
import re
from datetime import datetime, date
from sqlalchemy import Integer, String, Text, Date, DateTime, ForeignKey, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column
import storage

CATEGORIES = {'ultrasound':'УЗИ','mri':'МРТ','ct':'КТ','xray':'Рентген','cbc':'Общий анализ крови','biochemistry':'Биохимия крови','urine':'Анализы мочи','other':'Другие исследования'}

def classify_document(text):
    text=(text or '').lower().replace('ё','е')
    rules=[('ultrasound',r'\bузи\b|ультразвук|\bultrasound\b'),('mri',r'\bмрт\b|магнитно.резонанс|\bmri\b|magnetic resonance'),('ct',r'\bкт\b|компьютерн.{0,8}томограф|\bct\b|computed tomography'),('xray',r'рентген|\bx.ray\b|radiograph'),('urine',r'моч[аиу]|\bоам\b|\burine\b|urinalysis'),('biochemistry',r'биохим|\bбх\b|biochem|blood chemistry'),('cbc',r'общ.{0,8}анализ крови|\bоак\b|гематолог|\bcbc\b|complete blood count|h[ae]ematolog')]
    for key,pattern in rules:
        if re.search(pattern,text):return key
    return 'other'

def plain_text(text):
    text=str(text or '')
    text=re.sub(r'```[^\n]*\n?', '', text)
    text=re.sub(r'!\[([^\]]*)\]\([^)]*\)',r'\1',text)
    text=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'\1 (\2)',text)
    text=re.sub(r'(?m)^\s{0,3}#{1,6}\s*','',text)
    text=re.sub(r'(?m)^\s*\*\s+','• ',text)
    text=text.replace('**','').replace('__','').replace('`','')
    text=re.sub(r'(?<!\w)\*([^*\n]+)\*(?!\w)',r'\1',text)
    return text.strip()

class DocumentLabel(storage.Base):
    __tablename__='document_labels'
    source: Mapped[str]=mapped_column(String(10),primary_key=True)
    document_id: Mapped[int]=mapped_column(Integer,primary_key=True)
    category: Mapped[str]=mapped_column(String(40),default='other')
    study_date: Mapped[date|None]=mapped_column(Date,nullable=True)

class PetPhoto(storage.Base):
    __tablename__='pet_photos'
    pet_id: Mapped[int]=mapped_column(ForeignKey('pets.id'),primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),index=True)
    data: Mapped[bytes]=mapped_column(LargeBinary)
    mime_type: Mapped[str]=mapped_column(String(60))
    updated_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)

class OperationRecord(storage.Base):
    __tablename__='pet_operations'
    id: Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),index=True)
    pet_id: Mapped[int]=mapped_column(ForeignKey('pets.id'),index=True)
    title: Mapped[str]=mapped_column(String(255))
    operation_date: Mapped[date|None]=mapped_column(Date,nullable=True)
    notes: Mapped[str|None]=mapped_column(Text,nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)

class OperationAttachment(storage.Base):
    __tablename__='operation_attachments'
    id: Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True)
    operation_id: Mapped[int]=mapped_column(ForeignKey('pet_operations.id'),index=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),index=True)
    filename: Mapped[str]=mapped_column(String(255))
    mime_type: Mapped[str]=mapped_column(String(100))
    data: Mapped[bytes|None]=mapped_column(LargeBinary,nullable=True)
    telegram_file_id: Mapped[str|None]=mapped_column(Text,nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
