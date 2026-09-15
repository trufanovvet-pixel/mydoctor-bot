from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from storage import Base, Consultation, SessionLocal, User, engine


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    modality: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True, nullable=False)


class ConversationState(Base):
    __tablename__ = "conversation_states"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def init_conversation_store() -> None:
    Base.metadata.create_all(engine)


def _get_or_create_user(session, telegram_id: int) -> User:
    user = session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id)
        session.add(user)
        session.flush()
    return user


def save_conversation_message(
    telegram_id: int,
    role: str,
    content: str,
    modality: str = "text",
) -> None:
    text = (content or "").strip()
    if role not in {"user", "assistant"} or not text:
        return
    with SessionLocal() as session:
        user = _get_or_create_user(session, telegram_id)
        session.add(
            ConversationMessage(
                user_id=user.id,
                telegram_id=telegram_id,
                role=role,
                content=text,
                modality=modality,
            )
        )
        session.commit()


def _legacy_history(session, telegram_id: int, limit: int) -> list[dict]:
    user = session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        return []

    state = session.get(ConversationState, telegram_id)
    query = (
        select(Consultation)
        .where(
            Consultation.user_id == user.id,
            Consultation.kind.in_(["chat", "voice_chat", "file_analysis"]),
        )
        .order_by(Consultation.id.desc())
        .limit(max(1, (limit + 1) // 2))
    )
    if state is not None and state.reset_at is not None:
        query = query.where(Consultation.created_at > state.reset_at)

    items = list(reversed(session.scalars(query).all()))
    history: list[dict] = []
    for item in items:
        if item.user_text and item.user_text.strip():
            history.append({"role": "user", "content": item.user_text.strip()})
        if item.assistant_text and item.assistant_text.strip():
            history.append({"role": "assistant", "content": item.assistant_text.strip()})
    return history[-limit:]


def load_conversation_history(telegram_id: int, limit: int = 12) -> list[dict]:
    safe_limit = max(1, min(int(limit), 30))
    with SessionLocal() as session:
        rows = session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.telegram_id == telegram_id)
            .order_by(ConversationMessage.id.desc())
            .limit(safe_limit)
        ).all()
        if rows:
            rows = list(reversed(rows))
            return [{"role": row.role, "content": row.content} for row in rows]
        return _legacy_history(session, telegram_id, safe_limit)


def clear_conversation_history(telegram_id: int) -> None:
    with SessionLocal() as session:
        session.execute(
            delete(ConversationMessage).where(ConversationMessage.telegram_id == telegram_id)
        )
        state = session.get(ConversationState, telegram_id)
        if state is None:
            state = ConversationState(telegram_id=telegram_id, reset_at=datetime.utcnow())
            session.add(state)
        else:
            state.reset_at = datetime.utcnow()
        session.commit()
