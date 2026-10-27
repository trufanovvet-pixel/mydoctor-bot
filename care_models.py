"""Additive, shared care records. Existing patient and billing tables are unchanged."""
from datetime import date, datetime
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
import storage


class CareMember(storage.Base):
    __tablename__ = 'care_members'
    pet_id: Mapped[int] = mapped_column(ForeignKey('pets.id'), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CareInvite(storage.Base):
    __tablename__ = 'care_invites'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pet_id: Mapped[int] = mapped_column(ForeignKey('pets.id'), index=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class CarePreference(storage.Base):
    __tablename__ = 'care_preferences'
    pet_id: Mapped[int] = mapped_column(ForeignKey('pets.id'), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    reminders: Mapped[bool] = mapped_column(Boolean, default=False)


class CareSharing(storage.Base):
    __tablename__ = 'care_sharing'
    pet_id: Mapped[int] = mapped_column(ForeignKey('pets.id'), primary_key=True)
    doctor_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class CarePrescription(storage.Base):
    __tablename__ = 'care_prescriptions'
    # The original is a WebDocument; no cross-process FK to the web-only model.
    document_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pet_id: Mapped[int] = mapped_column(ForeignKey('pets.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    medicines: Mapped[list] = mapped_column(JSON)


class MedicationCourse(storage.Base):
    __tablename__ = 'care_medication_courses'
    __table_args__ = (UniqueConstraint('created_by', 'request_key'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pet_id: Mapped[int] = mapped_column(ForeignKey('pets.id'), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    request_key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(160))
    dose: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str] = mapped_column(Text, default='')
    timezone: Mapped[str] = mapped_column(String(80))
    times: Mapped[list] = mapped_column(JSON)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    prescription_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)


class MedicationDose(storage.Base):
    __tablename__ = 'care_medication_doses'
    __table_args__ = (UniqueConstraint('course_id', 'due_at'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey('care_medication_courses.id'), index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    state: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    marked_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    marked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class HealthEntry(storage.Base):
    __tablename__ = 'care_health_entries'
    __table_args__ = (UniqueConstraint('created_by', 'request_key'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pet_id: Mapped[int] = mapped_column(ForeignKey('pets.id'), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    request_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    operation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    condition: Mapped[str] = mapped_column(String(160), default='')
    observed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    timezone: Mapped[str] = mapped_column(String(80))
    appetite: Mapped[str] = mapped_column(String(20))
    activity: Mapped[str] = mapped_column(String(20))
    pain: Mapped[str] = mapped_column(String(20))
    wound: Mapped[str] = mapped_column(String(20), default='not_applicable')
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    vomiting: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seizures: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default='')
    urgency: Mapped[str] = mapped_column(String(16), default='recorded')
    photo: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CareNotice(storage.Base):
    __tablename__ = 'care_notices'
    __table_args__ = (UniqueConstraint('dose_id', 'user_id', 'channel', 'destination'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dose_id: Mapped[int] = mapped_column(ForeignKey('care_medication_doses.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    channel: Mapped[str] = mapped_column(String(12))
    destination: Mapped[str] = mapped_column(String(80))
    binding: Mapped[str] = mapped_column(String(64), default='')
    state: Mapped[str] = mapped_column(String(16), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
