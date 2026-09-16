from telegram import ReplyKeyboardMarkup


RECORDS_MENU = ReplyKeyboardMarkup(
    [
        ["📤 Загрузить анализы/документы", "📋 История обращений"],
        ["⬅️ Главное меню"],
    ],
    resize_keyboard=True,
)


def install(bot):
    bot.MENU = ReplyKeyboardMarkup(
        [
            ["🐾 Мои питомцы", "🧪 Анализы и документы"],
            ["🛡 Профилактика", "👨‍⚕️ Записаться на консультацию"],
        ],
        resize_keyboard=True,
    )

    original_message = bot.message

    async def records_hub_message(update, context):
        text = (update.message.text or "").strip() if update.message else ""

        if text == "🧪 Анализы и документы":
            await update.message.reply_text(
                "🧪 Анализы и документы\n\nЗдесь можно загрузить новые анализы и медицинские документы или открыть историю обращений питомца.",
                reply_markup=RECORDS_MENU,
            )
            return

        if text == "📤 Загрузить анализы/документы":
            context.user_data["choice_flow"] = "media"
            context.user_data.pop("records_target_explicit", None)
            context.user_data.pop("records_target_pet_id", None)
            await update.message.reply_text(
                "Кому принадлежат анализы или документы?",
                reply_markup=ReplyKeyboardMarkup(
                    [["🐾 Привязать к питомцу", "🌐 Без привязки"], ["❌ Отмена"]],
                    resize_keyboard=True,
                    one_time_keyboard=True,
                ),
            )
            return

        if text == "⬅️ Главное меню":
            await update.message.reply_text(
                "Главное меню\n\nЧтобы задать вопрос, просто напишите его сообщением — отдельная кнопка не нужна.",
                reply_markup=bot.MENU,
            )
            return

        # Любой обычный текст, который не является кнопкой раздела,
        # передаём дальше как ветеринарный вопрос.
        return await original_message(update, context)

    bot.message = records_hub_message
