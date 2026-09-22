import asyncio
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Text, select
from sqlalchemy.orm import Mapped, mapped_column
from telegram import ReplyKeyboardMarkup

import conversation_store
import storage


class UserFeedback(storage.Base):
    __tablename__ = "user_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    context_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True, nullable=False)


def _save_feedback(telegram_id: int, comment: str, context_text: str):
    with storage.SessionLocal() as session:
        user = session.scalar(select(storage.User).where(storage.User.telegram_id == telegram_id))
        if user is None:
            user = storage.User(telegram_id=telegram_id)
            session.add(user)
            session.flush()
        row = UserFeedback(
            user_id=user.id,
            telegram_id=telegram_id,
            comment=comment.strip(),
            context_text=context_text.strip() or None,
        )
        session.add(row)
        session.commit()
        return row.id


def _context_text(telegram_id: int) -> str:
    history = conversation_store.load_conversation_history(telegram_id, limit=8)
    lines = []
    for item in history:
        role = "Пользователь" if item.get("role") == "user" else "Бот"
        value = str(item.get("content") or "").strip()
        if value:
            lines.append(f"{role}: {value}")
    return "\n\n".join(lines)[-12000:]


def install(bot):
    storage.Base.metadata.create_all(storage.engine)

    # Add feedback button without disturbing the current menu layout.
    rows = [list(row) for row in getattr(bot.MENU, "keyboard", [])]
    label = "🐞 Сообщить об ошибке"
    if not any(label in row for row in rows):
        rows.append([label])
        bot.MENU = ReplyKeyboardMarkup(rows, resize_keyboard=True)

    original_message = bot.message

    async def feedback_message(update, context):
        text = (update.message.text or "").strip() if update.message else ""

        if text == label:
            context.user_data["feedback_waiting"] = True
            await update.message.reply_text(
                "Напишите одним сообщением, что произошло: что было непонятно, неверно или не сработало. Я сохраню ваш комментарий вместе с последними сообщениями диалога.",
                reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True, one_time_keyboard=True),
            )
            return

        if context.user_data.get("feedback_waiting"):
            if text == "❌ Отмена":
                context.user_data.pop("feedback_waiting", None)
                await update.message.reply_text("Отменено.", reply_markup=bot.MENU)
                return
            if not text:
                await update.message.reply_text("Напишите проблему текстом одним сообщением.")
                return

            context_text = await asyncio.to_thread(_context_text, update.effective_user.id)
            feedback_id = await asyncio.to_thread(
                _save_feedback, update.effective_user.id, text, context_text
            )
            context.user_data.pop("feedback_waiting", None)
            await update.message.reply_text(
                f"Спасибо. Сообщение об ошибке сохранено №{feedback_id}.",
                reply_markup=bot.MENU,
            )
            return

        return await original_message(update, context)

    bot.message = feedback_message
