import asyncio
from telegram import ReplyKeyboardMarkup

import prevention_patch


PREVENTION_MENU = ReplyKeyboardMarkup(
    [
        ["💉 Вакцинация", "🪱 Гельминты"],
        ["🪲 Блохи и клещи", "📅 Календарь"],
        ["⬅️ Главное меню"],
    ],
    resize_keyboard=True,
)

CALENDAR_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Вакцинация", "➕ Обработка от гельминтов"],
        ["➕ Блохи/клещи", "📅 Мои события"],
        ["⬅️ Профилактика", "⬅️ Главное меню"],
    ],
    resize_keyboard=True,
)

HELMINTH_TEXT = """🪱 Гельминты — что важно знать

Основные группы:
• Нематоды — круглые гельминты: Toxocara, Ancylostoma, Trichuris и др.
• Цестоды — ленточные: Dipylidium, Taenia, Echinococcus.
• Трематоды — сосальщики; встречаются реже и требуют отдельного подбора препарата.
• Giardia и другие простейшие — НЕ гельминты. Обычная профилактическая обработка от гельминтов их не «закрывает».

Формы препаратов
1. Таблетки внутрь — самый универсальный вариант для большинства собак и кошек.
2. Капли на холку — удобны, если питомцу трудно дать таблетку; спектр зависит от действующего вещества.
3. Суспензии/пасты — удобны щенкам и котятам, но также отличаются по спектру.

Что обычно выбирают
• Нематоды + цестоды: комбинированный препарат широкого спектра удобнее монопрепарата.
  Примеры комбинаций: празиквантел + пирантел/фебантел; празиквантел + мильбемицин.
  Примеры торговых названий: Drontal/Дронтал, Milbemax/Мильбемакс — состав зависит от конкретной версии и страны.
• Нематоды: используются, в зависимости от показаний и вида животного, пирантел, фенбендазол, мильбемицин, моксидектин.
  Примеры: Panacur/Панакур (фенбендазол), препараты моксидектина.
• Капли на холку у кошек: Profender (эмодепсид + празиквантел); существуют и комбинированные средства с моксидектином или другими компонентами.
• Для кошек есть комбинированные spot-on препараты, одновременно направленные на несколько групп паразитов, например NexGard Combo (эсафоксоланер + эприномектин + празиквантел) в странах, где он зарегистрирован.

Что «лучше»
Нет одного лучшего препарата для всех. Лучше тот, который:
• зарегистрирован для данного вида и возраста;
• рассчитан на фактическую массу;
• закрывает именно нужную группу паразитов;
• подходит по образу жизни питомца.
Если нужен обычный широкий кишечный спектр у взрослого животного, чаще удобнее комбинация против нематод + цестод, чем препарат только против одной группы.

Когда риск выше
Охота и поедание добычи, сырое мясо, блохи, частые поездки, содержание нескольких животных, контакт с почвой/фекалиями и эндемичные районы меняют схему. При риске дирофиляриоза нужна отдельная профилактическая стратегия — обычная таблетка «от кишечных гельминтов» её не заменяет.

Важно: конкретный препарат и интервал выбирают по инструкции и риску; торговые названия выше — примеры, а регистрация и состав могут отличаться по стране и версии препарата.

Источники:
ESCCAP GL1 Worm Control in Dogs and Cats — https://www.esccap.org/guidelines/gl1/
ESCCAP MG1 quick guide — https://www.esccap.org/modular-guidelines/mg1/
CAPC Ascarid guideline — https://capcvet.org/guidelines/ascarid/
CAPC Trichuris guideline — https://capcvet.org/guidelines/trichuris-vulpis/
"""


def _clean_user_text(text: str) -> str:
    return (
        (text or "")
        .replace("обработка от глистов", "обработка от гельминтов")
        .replace("Обработка от глистов", "Обработка от гельминтов")
        .replace("глисты", "гельминты")
        .replace("Глисты", "Гельминты")
    )


def install(bot):
    # Keep legacy prevention code internally compatible, but expose only the preferred terminology.
    prevention_patch.PREVENTION_MENU = PREVENTION_MENU
    prevention_patch.CALENDAR_MENU = CALENDAR_MENU
    prevention_patch.WORM_TEXT = HELMINTH_TEXT

    original_due_notifications = prevention_patch._due_notifications

    def due_notifications_clean():
        rows = original_due_notifications()
        return [(event_id, field, chat_id, _clean_user_text(text)) for event_id, field, chat_id, text in rows]

    prevention_patch._due_notifications = due_notifications_clean

    original_message = bot.message

    async def terminology_message(update, context):
        text = (update.message.text or "").strip() if update.message else ""
        telegram_id = update.effective_user.id

        if text in {"🛡 Профилактика", "⬅️ Профилактика"}:
            context.user_data.pop("prevention_flow", None)
            context.user_data["prevention_section"] = "main"
            await update.message.reply_text(
                "🛡 Профилактика\n\nВыберите раздел:",
                reply_markup=PREVENTION_MENU,
            )
            return

        if text in {"🪱 Гельминты", "🪱 Глисты"}:
            await update.message.reply_text(HELMINTH_TEXT, reply_markup=PREVENTION_MENU)
            return

        if text == "📅 Календарь":
            context.user_data["prevention_section"] = "calendar"
            pet = await asyncio.to_thread(prevention_patch._active_pet, telegram_id)
            if not pet:
                await update.message.reply_text(
                    "Сначала выберите питомца в разделе «🐾 Мои питомцы», затем откройте календарь.",
                    reply_markup=bot.MENU,
                )
                return
            await update.message.reply_text(
                f"📅 Календарь профилактики: {pet['name']}\n\nЧто добавить?",
                reply_markup=CALENDAR_MENU,
            )
            return

        if text in {"➕ Обработка от гельминтов", "➕ Обработка от глистов"}:
            pet = await asyncio.to_thread(prevention_patch._active_pet, telegram_id)
            if not pet:
                await update.message.reply_text("Сначала выберите питомца.", reply_markup=bot.MENU)
                return
            context.user_data["prevention_flow"] = {
                "step": "date",
                "kind": "deworming",
                "label": "обработка от гельминтов",
            }
            await update.message.reply_text(
                f"Введите дату для {pet['name']} в формате ДД.ММ.ГГГГ, например 25.10.2026.",
                reply_markup=CALENDAR_MENU,
            )
            return

        if text == "📅 Мои события":
            items = await asyncio.to_thread(prevention_patch._list_events, telegram_id)
            if not items:
                await update.message.reply_text("В календаре пока нет будущих событий.", reply_markup=CALENDAR_MENU)
                return
            labels = {
                "vaccination": "вакцинация",
                "deworming": "обработка от гельминтов",
                "ectoparasites": "обработка от блох/клещей",
            }
            lines = ["📅 Ближайшие события:"]
            for item in items:
                lines.append(
                    f"{item['due_date'].strftime('%d.%m.%Y')} — {item['pet']}: "
                    f"{labels.get(item['kind'], item['kind'])}"
                )
            await update.message.reply_text("\n".join(lines), reply_markup=CALENDAR_MENU)
            return

        return await original_message(update, context)

    bot.message = terminology_message
