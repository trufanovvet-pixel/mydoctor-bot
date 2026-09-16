import asyncio
import calendar
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import prevention_patch


MOSCOW = ZoneInfo("Europe/Moscow")


def _today():
    return datetime.now(MOSCOW).date()


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е").strip()


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _parse_relative_date(text: str):
    value = _norm(text)
    today = _today()

    if "послезавтра" in value:
        return today + timedelta(days=2)
    if "завтра" in value:
        return today + timedelta(days=1)
    if "через неделю" in value:
        return today + timedelta(days=7)
    if "через месяц" in value:
        return _add_months(today, 1)
    if "через год" in value:
        return _add_months(today, 12)

    match = re.search(r"через\s+(\d+)\s*(дн(?:я|ей)?|день)", value)
    if match:
        return today + timedelta(days=int(match.group(1)))
    match = re.search(r"через\s+(\d+)\s*нед", value)
    if match:
        return today + timedelta(weeks=int(match.group(1)))
    match = re.search(r"через\s+(\d+)\s*месяц", value)
    if match:
        return _add_months(today, int(match.group(1)))
    match = re.search(r"через\s+(\d+)\s*(?:год|года|лет)", value)
    if match:
        return _add_months(today, int(match.group(1)) * 12)

    explicit = prevention_patch._parse_date(text)
    if explicit and explicit >= today:
        return explicit
    return None


def _event_from_text(text: str):
    value = _norm(text)
    if any(x in value for x in ("блох", "клещ")):
        return "ectoparasites", "обработка от блох/клещей"
    if any(x in value for x in ("гельминт", "дегельминт", "глист")):
        return "deworming", "обработка от гельминтов"
    if any(x in value for x in ("вакцинац", "привив")):
        return "vaccination", "вакцинация"
    return None


def _is_calendar_command(text: str) -> bool:
    value = _norm(text)
    action = any(x in value for x in ("добав", "заплан", "постав", "напом", "занес", "внес"))
    return action and _event_from_text(value) is not None and _parse_relative_date(value) is not None


def _find_pet(bot, telegram_id: int, text: str):
    pets = bot.list_pets(telegram_id)
    value = _norm(text)
    explicit = []
    for pet in pets:
        name = _norm(pet.get("name") or "")
        if name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", value):
            explicit.append(pet)
    if len(explicit) == 1:
        return explicit[0]
    if len(pets) == 1:
        return pets[0]
    active = bot.get_active_pet(telegram_id)
    if active:
        return active
    return None


VACCINATION_GUIDE = """💉 Вакцинация: кратко и по делу

СОБАКИ

WSAVA 2024
Щенки: core-вакцины против чумы, аденовируса и парвовируса начинают обычно в 6–8 недель и повторяют каждые 2–4 недели. Последняя доза первичной серии — в 16 недель или позже. Следующая ревакцинация рекомендована с 26 недель и старше. У взрослых животных core-компоненты не нужно автоматически вводить ежегодно: интервал зависит от конкретной вакцины и длительности иммунитета.
Бешенство: по законодательству и инструкции конкретной вакцины. Лептоспироз — по региональному риску и доступной вакцине.

Мультикан-8 — по инструкции производителя
Щенки: 8–10 недель → повтор через 21–28 суток → ревакцинация в 10–12 месяцев.
Взрослые собаки: 1 раз в год.
Важно: в августе 2026 производитель сообщил о приостановке реализации отдельных серий Мультикан-8 и Мультикан-6. Перед применением нужно проверить серию и актуальное сообщение производителя.

КОШКИ

WSAVA 2024
Котята: core-вакцины против панлейкопении, герпесвируса и калицивируса начинают обычно в 6–8 недель и повторяют каждые 2–4 недели. Последняя доза первичной серии — в 16 недель или позже. Следующая ревакцинация — с 26 недель и старше. У взрослых кошек core-компоненты не нужно автоматически вводить ежегодно, если конкретная вакцина обеспечивает более длительный иммунитет.
Бешенство — по законодательству и инструкции. FeLV особенно важна для молодых кошек и животных с риском контакта.

Мультифел — по инструкции производителя
Котята: с 8–12 недель, 2 введения с интервалом 21–28 суток.
Ранее не вакцинированные взрослые кошки: 2 введения с интервалом 21–28 суток.
Ревакцинация: ежегодно.
Важно: начиная с серии 11 от 02.2026 Мультифел выпускается без хламидиозного компонента.

Если пришлёте фото ветпаспорта, бот сможет разобрать даты и названия вакцин и подготовить календарь следующей профилактики для подтверждения владельцем."""


def install(bot):
    original_message = bot.message

    async def smart_prevention_message(update, context):
        text = (update.message.text or "").strip() if update.message else ""
        value = _norm(text)

        if _is_calendar_command(text):
            await bot.ensure_current_user(update)
            pet = await asyncio.to_thread(_find_pet, bot, update.effective_user.id, text)
            if pet is None:
                await update.message.reply_text(
                    "Не понял, для какого питомца создать напоминание. Укажите имя питомца в сообщении.",
                    reply_markup=bot.MENU,
                )
                return
            parsed = _event_from_text(text)
            due = _parse_relative_date(text)
            kind, label = parsed
            ok = await asyncio.to_thread(
                prevention_patch._save_event,
                update.effective_user.id,
                pet["id"],
                kind,
                due,
                "Добавлено обычным сообщением",
            )
            if ok:
                await update.message.reply_text(
                    f"Готово ✅\n{pet['name']}: {label} — {due.strftime('%d.%m.%Y')}.\n"
                    "Напомню за 7 дней, за 1 день и в сам день.",
                    reply_markup=bot.MENU,
                )
            else:
                await update.message.reply_text("Не удалось сохранить событие. Попробуйте ещё раз.", reply_markup=bot.MENU)
            return

        vaccination_query = (
            text == "💉 Вакцинация"
            or ("вакцинац" in value and any(x in value for x in ("схем", "когда", "щен", "котен", "взросл")))
        )
        if vaccination_query:
            await update.message.reply_text(VACCINATION_GUIDE, reply_markup=prevention_patch.PREVENTION_MENU)
            return

        return await original_message(update, context)

    bot.message = smart_prevention_message
