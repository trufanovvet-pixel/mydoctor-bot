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

HELMINTH_TEXT = """🪱 Гельминты

Для щенков и котят профилактика обычно требуется чаще, чем для взрослых животных.

У взрослых животных схема зависит от риска: прогулки, охота, сырое мясо, поедание добычи, контакт с другими животными и поездки.

Что делать на практике:
• использовать только препарат, подходящий виду и массе животного;
• соблюдать интервал из инструкции именно к выбранному препарату;
• при высоком риске обсуждать более частую профилактику или контроль кала;
• если обработка пропущена — не удваивать дозу, а провести её по инструкции и заново считать следующий срок.

Следующую дату можно сразу добавить в календарь — бот напомнит заранее."""


def install(bot):
    original_message = bot.message

    async def terminology_message(update, context):
        text = (update.message.text or "").strip() if update.message else ""
        telegram_id = update.effective_user.id

        if text in {"🛡 Профилактика", "⬅️ Профилактика"}:
            await update.message.reply_text(
                "🛡 Профилактика\n\nВыберите раздел:",
                reply_markup=PREVENTION_MENU,
            )
            return

        if text == "🪱 Гельминты":
            await update.message.reply_text(HELMINTH_TEXT, reply_markup=PREVENTION_MENU)
            return

        if text == "📅 Календарь":
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

        if text == "➕ Обработка от гельминтов":
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

        return await original_message(update, context)

    bot.message = terminology_message
