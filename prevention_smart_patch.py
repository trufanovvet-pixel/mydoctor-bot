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
    try:
        return _parse_relative_date_unchecked(text)
    except (ValueError, OverflowError):
        return None


def _parse_relative_date_unchecked(text: str):
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

    date_match = re.search(r"\b(?:\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{2}-\d{2})\b", text)
    explicit = prevention_patch._parse_date(date_match.group() if date_match else text)
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


VACCINATION_GUIDE = """💉 Вакцинация — конкретная схема

СОБАКИ

WSAVA 2024 — базовые core-вакцины: чума + аденовирус + парвовирус
Щенок:
6 недель — можно начинать первую вакцинацию.
9–10 недель — следующая доза.
12–14 недель — следующая доза.
16 недель или старше — обязательная завершающая доза щенячьей серии.
Высокий риск инфекции — продолжать до 20 недель, интервал 2–3 недели.
Около 6 месяцев — ревакцинация.
3 года — следующая ревакцинация core-компонентов.
Далее — не чаще 1 раза в 3 года для core-компонентов.

Если взрослая собака старше 16 недель ранее не вакцинирована:
по WSAVA одной дозы качественной MLV/recombinant core-вакцины обычно достаточно для защиты большинства собак, хотя производитель конкретной вакцины может требовать 2 дозы с интервалом 2–4 недели.

Бешенство:
по законодательству РФ и инструкции конкретной вакцины; интервал не подменяем схемой WSAVA.

Мультикан-8 — по инструкции производителя
Щенок:
8–10 недель — первая доза.
Через 21–28 дней — вторая доза.
10–12 месяцев — ревакцинация.
Взрослая собака — ежегодная ревакцинация.

КОШКИ

WSAVA 2024 — базовые core-вакцины: панлейкопения + герпесвирус + калицивирус
Котёнок:
6 недель — можно начинать первую вакцинацию.
9–10 недель — следующая доза.
12–14 недель — следующая доза.
16 недель или старше — обязательная завершающая доза котячьей серии.
Высокий риск инфекции — продолжать до 20 недель, интервал 2–3 недели.
Около 6 месяцев — ревакцинация.
3 года — следующая ревакцинация у кошек низкого риска.
Далее — не чаще 1 раза в 3 года у кошек низкого риска.
Кошкам высокого риска может требоваться более частая ревакцинация, вплоть до ежегодной.

Если взрослая кошка старше 16 недель ранее не вакцинирована:
обычно используют 2 дозы с интервалом 2–4 недели. Для инактивированных core-вакцин 2 дозы обязательны для первичной иммунизации.

Бешенство:
по законодательству РФ и инструкции конкретной вакцины.

Мультифел — по инструкции производителя
Котёнок:
8–12 недель — первая доза.
Через 21–28 дней — вторая доза.
Далее — ежегодная ревакцинация.
Ранее не вакцинированная взрослая кошка — 2 дозы с интервалом 21–28 дней, далее ежегодно.

Важно: начиная с серии 11 от 02.2026 Мультифел выпускается без хламидиозного компонента.

Если пришлёте фото ветпаспорта, бот сможет прочитать даты и названия вакцин и предложить готовый календарь следующих вакцинаций для подтверждения владельцем."""


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
