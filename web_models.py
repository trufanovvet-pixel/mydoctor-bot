from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
import storage

class WebAccount(storage.Base):
    __tablename__="web_accounts"
    id: Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True)
    user_id: Mapped[int]=mapped_column(ForeignKey("users.id"),unique=True,index=True,nullable=False)
    email: Mapped[str]=mapped_column(String(320),unique=True,index=True,nullable=False)
    password_hash: Mapped[str]=mapped_column(String(255),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow,nullable=False)

class PetMedicalProfile(storage.Base):
    __tablename__="pet_medical_profiles"
    pet_id: Mapped[int]=mapped_column(ForeignKey("pets.id"),primary_key=True)
    birth_date: Mapped[str|None]=mapped_column(String(32),nullable=True)
    neutered: Mapped[str|None]=mapped_column(String(32),nullable=True)
    chronic_conditions: Mapped[str|None]=mapped_column(Text,nullable=True)
    allergies: Mapped[str|None]=mapped_column(Text,nullable=True)
    medications: Mapped[str|None]=mapped_column(Text,nullable=True)
    surgeries: Mapped[str|None]=mapped_column(Text,nullable=True)
    important_diagnoses: Mapped[str|None]=mapped_column(Text,nullable=True)
    photo_path: Mapped[str|None]=mapped_column(Text,nullable=True)

class WebMessage(storage.Base):
    __tablename__="web_messages"
    id: Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True)
    user_id: Mapped[int]=mapped_column(ForeignKey("users.id"),index=True,nullable=False)
    pet_id: Mapped[int]=mapped_column(ForeignKey("pets.id"),index=True,nullable=False)
    role: Mapped[str]=mapped_column(String(16),nullable=False)
    content: Mapped[str]=mapped_column(Text,nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow,index=True,nullable=False)

class WebDocument(storage.Base):
    __tablename__="web_documents"
    id: Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True)
    user_id: Mapped[int]=mapped_column(ForeignKey("users.id"),index=True,nullable=False)
    pet_id: Mapped[int]=mapped_column(ForeignKey("pets.id"),index=True,nullable=False)
    filename: Mapped[str]=mapped_column(Text,nullable=False)
    mime_type: Mapped[str|None]=mapped_column(String(120),nullable=True)
    storage_path: Mapped[str]=mapped_column(Text,nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow,index=True,nullable=False)
