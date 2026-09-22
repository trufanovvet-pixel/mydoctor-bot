import secrets
from datetime import datetime, timedelta
from sqlalchemy import BigInteger, DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column
import storage

class WebLoginToken(storage.Base):
    __tablename__ = "web_login_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

def init():
    storage.Base.metadata.create_all(storage.engine)

def create_token(telegram_id: int, minutes: int = 10) -> str:
    init()
    token = secrets.token_urlsafe(32)
    with storage.SessionLocal() as session:
        session.add(WebLoginToken(token=token, telegram_id=telegram_id, expires_at=datetime.utcnow()+timedelta(minutes=minutes)))
        session.commit()
    return token

def consume_token(token: str) -> int | None:
    init()
    now=datetime.utcnow()
    with storage.SessionLocal() as session:
        row=session.scalar(select(WebLoginToken).where(WebLoginToken.token==token))
        if row is None or row.used_at is not None or row.expires_at < now:
            return None
        row.used_at=now
        session.commit()
        return row.telegram_id
