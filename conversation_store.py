from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, select, delete
from sqlalchemy.orm import Mapped, mapped_column

from storage import Base, SessionLocal, User, engine


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    modality: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True, nullable=False)


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


def load_conversation_history(telegram_id: int, limit: int = 12) -> list[dict]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.telegram_id == telegram_id)
            .order_by(ConversationMessage.id.desc())
            .limit(max(1, min(int(limit), 30)))
        ).all()
    rows = list(reversed(rows))
    return [{"role": row.role, "content": row.content} for row in rows]


def clear_conversation_history(telegram_id: int) -> None:
    with SessionLocal() as session:
        session.execute(
            delete(ConversationMessage).where(ConversationMessage.telegram_id == telegram_id)
        )
        session.commit()
