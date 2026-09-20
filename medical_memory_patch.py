import asyncio
import re
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, select
from sqlalchemy.orm import Mapped, mapped_column

import storage
import pet_records_patch


class PetDocumentInsight(storage.Base):
    __tablename__ = "pet_document_insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("pet_documents.id"), unique=True, index=True, nullable=False)
    analysis_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _is_compare_request(text: str) -> bool:
    value = _norm(text)
    compare = any(x in value for x in ("сравни", "динамик", "изменил", "изменения", "предыдущ"))
    medical = any(x in value for x in ("анализ", "результат", "показател", "кров", "моч", "биохим", "оак"))
    return compare and medical


def _pet_from_text(pets, text: str):
    return pet_records_patch._pet_from_text(pets, text)


def _latest_document_id(telegram_id: int, pet_id: int | None):
    with storage.SessionLocal() as session:
        user = session.scalar(select(storage.User).where(storage.User.telegram_id == telegram_id))
        if user is None:
            return None
        row = session.scalar(
            select(pet_records_patch.PetDocument)
            .where(
                pet_records_patch.PetDocument.user_id == user.id,
                pet_records_patch.PetDocument.pet_id == pet_id,
            )
            .order_by(pet_records_patch.PetDocument.created_at.desc())
        )
        return row.id if row else None


def _save_insight(document_id: int | None, analysis_text: str):
    if not document_id or not analysis_text.strip():
        return
    with storage.SessionLocal() as session:
        existing = session.scalar(
            select(PetDocumentInsight).where(PetDocumentInsight.document_id == document_id)
        )
        if existing is None:
            session.add(PetDocumentInsight(document_id=document_id, analysis_text=analysis_text.strip()))
        else:
            existing.analysis_text = analysis_text.strip()
        session.commit()


def _analysis_history(telegram_id: int, pet_id: int, limit: int = 8):
    with storage.SessionLocal() as session:
        user = session.scalar(select(storage.User).where(storage.User.telegram_id == telegram_id))
        if user is None:
            return []
        rows = session.execute(
            select(pet_records_patch.PetDocument, PetDocumentInsight)
            .join(PetDocumentInsight, PetDocumentInsight.document_id == pet_records_patch.PetDocument.id)
            .where(
                pet_records_patch.PetDocument.user_id == user.id,
                pet_records_patch.PetDocument.pet_id == pet_id,
                pet_records_patch.PetDocument.category.in_(["analysis", "document"]),
            )
            .order_by(
                pet_records_patch.PetDocument.document_date.desc(),
                pet_records_patch.PetDocument.created_at.desc(),
            )
            .limit(limit)
        ).all()
        result = []
        for doc, insight in rows:
            result.append({
                "document_date": doc.document_date or doc.created_at,
                "filename": doc.filename,
                "analysis_text": insight.analysis_text,
            })
        return result


def install(bot):
    storage.Base.metadata.create_all(storage.engine)

    original_message = bot.message
    original_media = bot.media

    async def memory_media(update, context):
        result = await original_media(update, context)
        if not isinstance(result, dict) or not result.get("document_id") or not result.get("analysis_text"):
            return result
        await asyncio.to_thread(_save_insight, result["document_id"], result["analysis_text"])
        return result

    async def memory_message(update, context):
        text = (update.message.text or "").strip() if update.message else ""
        if not _is_compare_request(text):
            return await original_message(update, context)

        await bot.ensure_current_user(update)
        pets = await asyncio.to_thread(bot.list_pets, update.effective_user.id)
        pet = _pet_from_text(pets, text)
        if pet is None and context.user_data.get("dialog_scope") == "pet":
            pet = await asyncio.to_thread(bot.get_active_pet, update.effective_user.id)
        if pet is None and len(pets) == 1:
            pet = pets[0]

        if pet is None:
            names = ", ".join(p["name"] for p in pets)
            await update.message.reply_text(
                f"Уточните питомца. Сохранены: {names}." if names else "Сначала добавьте питомца.",
                reply_markup=bot.MENU,
            )
            return

        rows = await asyncio.to_thread(_analysis_history, update.effective_user.id, pet["id"])
        if len(rows) < 2:
            await update.message.reply_text(
                f"Для {pet['name']} пока недостаточно сохранённых анализов с распознанными данными для сравнения. Нужны минимум два.",
                reply_markup=bot.MENU,
            )
            return

        blocks = []
        for row in rows[:6]:
            date = row["document_date"].strftime("%d.%m.%Y") if row.get("document_date") else "дата не определена"
            blocks.append(
                f"Сохранённая интерпретация ИИ документа от {date}, файл: {row.get('filename') or 'без названия'}\n{row['analysis_text']}"
            )

        prompt = (
            f"Сравни сохранённые лабораторные исследования питомца {pet['name']} в динамике. "
            "Ниже сохранённые интерпретации ИИ, а не оригиналы бланков и не подтверждённые владельцем факты. "
            "Обозначь сравнение как предварительное по сохранённым пересказам. "
            "Для клинических решений или сомнительных значений попроси оригиналы; "
            "не выдавай прежние выводы ИИ за подтверждённые результаты лаборатории. "
            "Используй только данные ниже. Не придумывай показатели, которых нет. "
            "Сначала краткий итог динамики, затем значимые показатели: что выросло, снизилось, "
            "нормализовалось или осталось стабильным. Учитывай единицы и референсы, если они указаны. "
            "Если показатели или единицы нельзя корректно сопоставить, прямо скажи об этом. "
            "В конце укажи, какие изменения требуют внимания. Пиши обычным текстом без Markdown.\n\n"
            + "\n\n---\n\n".join(blocks)
        )

        try:
            response = await asyncio.to_thread(
                bot.client.responses.create,
                model="gpt-5.6-sol",
                instructions=bot.SYSTEM_PROMPT,
                input=[{"role": "user", "content": prompt}],
            )
            answer = (response.output_text or "").strip()
        except Exception as exc:
            print(f"medical memory compare error: {exc!r}", flush=True)
            answer = "Не удалось сравнить сохранённые анализы. Попробуйте ещё раз."

        await update.message.reply_text(answer, reply_markup=bot.MENU)

    bot.media = memory_media
    bot.message = memory_message
