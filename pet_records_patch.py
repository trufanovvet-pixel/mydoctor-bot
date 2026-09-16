import asyncio
import re
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

import storage


MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "май": 5,
    "мая": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}


class PetDocument(storage.Base):
    __tablename__ = "pet_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    pet_id: Mapped[int | None] = mapped_column(ForeignKey("pets.id"), index=True, nullable=True)
    telegram_file_id: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    media_type: Mapped[str] = mapped_column(String(30), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(40), default="document", index=True, nullable=False)
    document_date: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True, nullable=False)


def _norm(value: str) -> str:
    return (value or "").lower().replace("ё", "е")


def _parse_year(raw: str) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 100:
        value += 2000
    if 2000 <= value <= 2100:
        return value
    return None


def _extract_document_date(text: str) -> datetime | None:
    value = _norm(text)

    patterns = (
        r"\b(20\d{2})[-./](0?[1-9]|1[0-2])[-./]([0-3]?\d)\b",
        r"\b([0-3]?\d)[-./](0?[1-9]|1[0-2])[-./](20\d{2}|\d{2})\b",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, value)
        if not match:
            continue
        try:
            if index == 0:
                year, month, day = map(int, match.groups())
            else:
                day = int(match.group(1))
                month = int(match.group(2))
                year = _parse_year(match.group(3))
                if year is None:
                    continue
            return datetime(year, month, day)
        except ValueError:
            pass

    for stem, month in MONTHS.items():
        match = re.search(
            rf"\b([0-3]?\d)?\s*{stem}[а-я]*\s+(20\d{{2}}|\d{{2}})(?:-?го)?\b",
            value,
        )
        if not match:
            continue
        year = _parse_year(match.group(2))
        if year is None:
            continue
        day = int(match.group(1) or 1)
        try:
            return datetime(year, month, day)
        except ValueError:
            pass
    return None


def _requested_period(text: str):
    value = _norm(text)
    month = None
    month_pos = None
    for stem, number in MONTHS.items():
        pos = value.find(stem)
        if pos >= 0:
            month = number
            month_pos = pos
            break

    year = None
    if month_pos is not None:
        nearby = value[max(0, month_pos - 8): month_pos + 28]
        match = re.search(r"\b(20\d{2}|\d{2})(?:-?го)?\b", nearby)
        if match:
            year = _parse_year(match.group(1))
    if year is None:
        match = re.search(r"\b(20\d{2})\b", value)
        if match:
            year = _parse_year(match.group(1))
    return month, year


def _looks_like_archive_request(text: str) -> bool:
    value = _norm(text)
    objects = ("анализ", "документ", "результат", "заключен", "выписк")
    actions = ("покаж", "скинь", "пришл", "отправ", "найд", "достань", "дай", "загрузи")
    return any(item in value for item in objects) and any(item in value for item in actions)


def _category(text: str) -> str:
    value = _norm(text)
    if any(x in value for x in ("анализ", "биохим", "общий анализ", "оак", "кров", "моч", "кал", "пцр")):
        return "analysis"
    if any(x in value for x in ("узи", "рентген", "кт ", "мрт", "томограф", "эндоскоп")):
        return "diagnostic_report"
    if any(x in value for x in ("назначен", "рецепт", "лист назнач")):
        return "prescription"
    return "document"


def _save_document(
    telegram_id: int,
    pet_id: int | None,
    file_id: str,
    unique_id: str | None,
    media_type: str,
    mime_type: str | None,
    filename: str | None,
    caption: str | None,
    category: str,
    document_date: datetime | None,
):
    with storage.SessionLocal() as session:
        user = session.scalar(select(storage.User).where(storage.User.telegram_id == telegram_id))
        if user is None:
            return False

        if pet_id is not None:
            pet = session.scalar(
                select(storage.Pet).where(storage.Pet.id == pet_id, storage.Pet.user_id == user.id)
            )
            if pet is None:
                pet_id = None

        if unique_id:
            existing = session.scalar(
                select(PetDocument).where(
                    PetDocument.user_id == user.id,
                    PetDocument.telegram_file_unique_id == unique_id,
                    PetDocument.pet_id == pet_id,
                )
            )
            if existing is not None:
                return False

        session.add(
            PetDocument(
                user_id=user.id,
                pet_id=pet_id,
                telegram_file_id=file_id,
                telegram_file_unique_id=unique_id,
                media_type=media_type,
                mime_type=mime_type,
                filename=filename,
                caption=caption,
                category=category,
                document_date=document_date,
            )
        )
        session.commit()
        return True


def _documents_for_pet(telegram_id: int, pet_id: int | None):
    with storage.SessionLocal() as session:
        user = session.scalar(select(storage.User).where(storage.User.telegram_id == telegram_id))
        if user is None:
            return []
        rows = session.scalars(
            select(PetDocument)
            .where(PetDocument.user_id == user.id, PetDocument.pet_id == pet_id)
            .order_by(PetDocument.document_date.desc(), PetDocument.created_at.desc())
        ).all()
        return [
            {
                "id": row.id,
                "file_id": row.telegram_file_id,
                "media_type": row.media_type,
                "mime_type": row.mime_type,
                "filename": row.filename,
                "caption": row.caption,
                "category": row.category,
                "document_date": row.document_date,
                "created_at": row.created_at,
            }
            for row in rows
        ]


def _pet_from_text(pets, text: str):
    value = _norm(text)
    matches = []
    for pet in pets:
        name = _norm(pet.get("name") or "").strip()
        if name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", value):
            matches.append(pet)
    return matches[0] if len(matches) == 1 else None


def _filter_period(items, month: int | None, year: int | None, analyses_only: bool):
    result = []
    for item in items:
        if analyses_only and item.get("category") not in {"analysis", "document"}:
            continue
        date = item.get("document_date") or item.get("created_at")
        if month is not None and (not date or date.month != month):
            continue
        if year is not None and (not date or date.year != year):
            continue
        result.append(item)
    return result


def install(bot):
    storage.Base.metadata.create_all(storage.engine)

    original_message = bot.message
    original_media = bot.media
    original_pet_callback = bot.pet_callback

    async def records_pet_callback(update, context):
        action = context.user_data.get("after_pet_action")
        result = await original_pet_callback(update, context)
        if action == "media":
            pet = await asyncio.to_thread(bot.get_active_pet, update.effective_user.id)
            context.user_data["records_target_explicit"] = True
            context.user_data["records_target_pet_id"] = pet["id"] if pet else None
        return result

    async def records_message(update, context):
        text = (update.message.text or "").strip() if update.message else ""

        if text == "🧪 Анализы и документы":
            context.user_data.pop("records_target_explicit", None)
            context.user_data.pop("records_target_pet_id", None)

        if text == "🌐 Без привязки":
            context.user_data["records_target_explicit"] = True
            context.user_data["records_target_pet_id"] = None

        if not _looks_like_archive_request(text):
            return await original_message(update, context)

        await bot.ensure_current_user(update)
        pets = await asyncio.to_thread(bot.list_pets, update.effective_user.id)
        pet = _pet_from_text(pets, text)

        if pet is None and context.user_data.get("dialog_scope") == "pet":
            pet = await asyncio.to_thread(bot.get_active_pet, update.effective_user.id)
        if pet is None and len(pets) == 1:
            pet = pets[0]

        if pet is None:
            if not pets:
                await update.message.reply_text(
                    "Сначала добавьте питомца, чтобы я мог вести его архив анализов.",
                    reply_markup=bot.MENU,
                )
            else:
                names = ", ".join(p["name"] for p in pets)
                await update.message.reply_text(
                    f"По какому питомцу показать документы? У вас сохранены: {names}.",
                    reply_markup=bot.MENU,
                )
            return

        month, year = _requested_period(text)
        items = await asyncio.to_thread(_documents_for_pet, update.effective_user.id, pet["id"])
        analyses_only = "анализ" in _norm(text) or "результат" in _norm(text)
        items = _filter_period(items, month, year, analyses_only)

        if not items:
            period = ""
            if month or year:
                period = " за указанный период"
            await update.message.reply_text(
                f"По питомцу {pet['name']} сохранённых файлов{period} не нашёл.",
                reply_markup=bot.MENU,
            )
            return

        shown = items[:10]
        await update.message.reply_text(
            f"Нашёл {len(items)} файл(ов) по питомцу {pet['name']}. Отправляю сохранённые документы.",
            reply_markup=bot.MENU,
        )
        for item in shown:
            date = item.get("document_date") or item.get("created_at")
            date_text = date.strftime("%d.%m.%Y") if date else "дата не определена"
            caption = f"{pet['name']} • {date_text}"
            if item.get("filename"):
                caption += f" • {item['filename']}"
            try:
                if item.get("media_type") == "photo":
                    await context.bot.send_photo(
                        chat_id=update.effective_chat.id,
                        photo=item["file_id"],
                        caption=caption[:1000],
                    )
                else:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=item["file_id"],
                        caption=caption[:1000],
                    )
            except Exception as exc:
                print(f"archive resend error document_id={item['id']}: {exc!r}", flush=True)

        if len(items) > len(shown):
            await update.message.reply_text(
                f"Показал первые {len(shown)} файлов из {len(items)}. Можно уточнить месяц или год.",
                reply_markup=bot.MENU,
            )

    async def records_media(update, context):
        message = update.message
        if message is None:
            return await original_media(update, context)

        file_id = None
        unique_id = None
        mime_type = None
        filename = None
        media_type = None
        if message.photo:
            item = message.photo[-1]
            file_id = item.file_id
            unique_id = item.file_unique_id
            mime_type = "image/jpeg"
            filename = "medical_image.jpg"
            media_type = "photo"
        elif message.document:
            item = message.document
            file_id = item.file_id
            unique_id = item.file_unique_id
            mime_type = item.mime_type
            filename = item.file_name or "medical_document"
            media_type = "document"

        if not file_id:
            return await original_media(update, context)

        before_history = list(context.user_data.get("history", []))
        result = await original_media(update, context)

        if context.user_data.get("records_target_explicit"):
            pet_id = context.user_data.get("records_target_pet_id")
        elif context.user_data.get("dialog_scope") == "pet":
            pet = await asyncio.to_thread(bot.get_active_pet, update.effective_user.id)
            pet_id = pet["id"] if pet else None
        else:
            pet_id = None

        history = context.user_data.get("history", [])
        assistant_text = ""
        if len(history) >= len(before_history):
            for item in reversed(history):
                if isinstance(item, dict) and item.get("role") == "assistant" and item.get("content"):
                    assistant_text = str(item.get("content"))
                    break

        caption = (message.caption or "").strip()
        combined = "\n".join(x for x in (filename or "", caption, assistant_text) if x)
        document_date = _extract_document_date(combined)
        category = _category(combined)

        saved = await asyncio.to_thread(
            _save_document,
            update.effective_user.id,
            pet_id,
            file_id,
            unique_id,
            media_type,
            mime_type,
            filename,
            caption,
            category,
            document_date,
        )

        if saved and pet_id is not None:
            pets = await asyncio.to_thread(bot.list_pets, update.effective_user.id)
            pet_name = next((p["name"] for p in pets if p["id"] == pet_id), "питомца")
            date_note = document_date.strftime(" от %d.%m.%Y") if document_date else ""
            await message.reply_text(
                f"Сохранил документ{date_note} в картотеку {pet_name}.",
                reply_markup=bot.MENU,
            )
        elif saved:
            await message.reply_text(
                "Документ сохранён в вашем архиве без привязки к питомцу.",
                reply_markup=bot.MENU,
            )
        return result

    bot.pet_callback = records_pet_callback
    bot.message = records_message
    bot.media = records_media
