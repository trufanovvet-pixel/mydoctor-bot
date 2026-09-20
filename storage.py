import os
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///mydoctor.db")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plan: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    paid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active_pet_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Pet(Base):
    __tablename__ = "pets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    species: Mapped[str] = mapped_column(String(40), nullable=False)
    breed: Mapped[str | None] = mapped_column(String(120), nullable=True)
    age: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sex: Mapped[str | None] = mapped_column(String(40), nullable=True)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Consultation(Base):
    __tablename__ = "consultations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    pet_id: Mapped[int | None] = mapped_column(ForeignKey("pets.id"), index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(40), default="chat", nullable=False)
    user_text: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True, nullable=False)


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True, nullable=False)


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


engine_kwargs = {"pool_pre_ping": True}
if _database_url().startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(_database_url(), **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


def ensure_user(telegram_id: int, username: str | None = None, first_name: str | None = None) -> dict:
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            user = User(telegram_id=telegram_id, username=username, first_name=first_name)
            session.add(user)
        else:
            user.username = username
            user.first_name = first_name
        session.commit()
        return _user_dict(user)


def _get_user(session: Session, telegram_id: int) -> User | None:
    return session.scalar(select(User).where(User.telegram_id == telegram_id))


def add_pet(
    telegram_id: int,
    name: str,
    species: str,
    breed: str | None,
    age: str | None,
    sex: str | None,
    weight_kg: float | None,
) -> dict:
    with SessionLocal() as session:
        user = _get_user(session, telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id)
            session.add(user)
            session.flush()
        pet = Pet(
            user_id=user.id,
            name=name,
            species=species,
            breed=breed,
            age=age,
            sex=sex,
            weight_kg=weight_kg,
        )
        session.add(pet)
        session.flush()
        user.active_pet_id = pet.id
        session.commit()
        return _pet_dict(pet)


def list_pets(telegram_id: int) -> list[dict]:
    with SessionLocal() as session:
        user = _get_user(session, telegram_id)
        if user is None:
            return []
        pets = session.scalars(select(Pet).where(Pet.user_id == user.id).order_by(Pet.created_at)).all()
        return [_pet_dict(pet) for pet in pets]


def set_active_pet(telegram_id: int, pet_id: int) -> dict | None:
    with SessionLocal() as session:
        user = _get_user(session, telegram_id)
        if user is None:
            return None
        pet = session.scalar(select(Pet).where(Pet.id == pet_id, Pet.user_id == user.id))
        if pet is None:
            return None
        user.active_pet_id = pet.id
        session.commit()
        return _pet_dict(pet)


def get_active_pet(telegram_id: int) -> dict | None:
    with SessionLocal() as session:
        user = _get_user(session, telegram_id)
        if user is None or user.active_pet_id is None:
            return None
        pet = session.scalar(select(Pet).where(Pet.id == user.active_pet_id, Pet.user_id == user.id))
        return _pet_dict(pet) if pet else None


def save_consultation(
    telegram_id: int,
    user_text: str,
    assistant_text: str,
    kind: str = "chat",
    pet_id: int | None | str = "active",
) -> None:
    with SessionLocal() as session:
        user = _get_user(session, telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id)
            session.add(user)
            session.flush()
        selected_pet_id = user.active_pet_id if pet_id == "active" else pet_id
        if selected_pet_id is not None and session.scalar(select(Pet.id).where(Pet.id == selected_pet_id, Pet.user_id == user.id)) is None:
            selected_pet_id = None
        item = Consultation(
            user_id=user.id,
            pet_id=selected_pet_id,
            kind=kind,
            user_text=user_text,
            assistant_text=assistant_text,
        )
        session.add(item)
        session.add(UsageEvent(user_id=user.id, event_type=kind))
        session.commit()


def set_bot_setting(key: str, value: str) -> None:
    with SessionLocal() as session:
        item = session.get(BotSetting, key)
        if item is None:
            item = BotSetting(key=key, value=value)
            session.add(item)
        else:
            item.value = value
            item.updated_at = datetime.utcnow()
        session.commit()


def get_bot_setting(key: str) -> str | None:
    with SessionLocal() as session:
        item = session.get(BotSetting, key)
        return item.value if item else None


def _user_dict(user: User) -> dict:
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "first_name": user.first_name,
        "plan": user.plan,
        "paid_until": user.paid_until,
        "active_pet_id": user.active_pet_id,
    }


def _pet_dict(pet: Pet) -> dict:
    return {
        "id": pet.id,
        "name": pet.name,
        "species": pet.species,
        "breed": pet.breed,
        "age": pet.age,
        "sex": pet.sex,
        "weight_kg": pet.weight_kg,
    }
