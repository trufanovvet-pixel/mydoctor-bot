import asyncio

from telegram import ReplyKeyboardMarkup


CAPABILITIES_TEXT = """ℹ️ Что умеет «МойДоктор»

🐾 Карточки питомцев
Храню данные ваших собак и кошек. В разделе «Мои питомцы» можно выбрать питомца или добавить нового.

📋 История обращений
Сохраняю предыдущие вопросы и разборы по каждому питомцу. История находится внутри раздела «Анализы и документы».

🧪 Анализы и документы
Можно отправлять фото, скриншоты и PDF. Я разберу документ и, если он привязан к питомцу, сохраню его в его картотеку.

🗂 Архив анализов
Можно попросить обычной фразой, например: «Покажи анализы Грома за май 2025» или «Пришли последние анализы Грома».

💬 Ветеринарные вопросы
Чтобы задать вопрос, просто напишите сообщение. Отдельную кнопку нажимать не нужно.

🎙 Можно говорить голосом
Не обязательно печатать — можно отправлять голосовые сообщения.

🛡 Профилактика
Можно вести календарь вакцинаций и обработок и получать напоминания.

🏥 Поиск ветеринарной помощи
Могу искать крупные ветеринарные центры и подбирать клинику под конкретную ситуацию.

👨‍⚕️ Онлайн-консультация
Через меню можно записаться на консультацию с врачом.

Пишите обычным человеческим языком — специальные команды запоминать не нужно."""


START_TEXT = """🐾 Привет! Я «МойДоктор» — ветеринарный помощник.

Я могу хранить карточки питомцев, историю обращений, анализы и медицинские документы, вести календарь профилактики, разбирать результаты исследований и помогать с ветеринарными вопросами.

Чтобы задать вопрос — просто напишите его сообщением. Отдельную кнопку выбирать не нужно.

Можно также отправлять голосовые, фото и PDF.

Примеры:
«У Грома рвота со вчерашнего дня»
«Покажи анализы Грома за май 2025»
«Чем биохимия отличается от общего анализа крови?»
«Посоветуй хорошую ветеринарную клинику в Туле»

Основные разделы находятся в меню ниже."""


def install(bot):
    bot.MENU = ReplyKeyboardMarkup(
        [
            ["🐾 Мои питомцы", "🧪 Анализы и документы"],
            ["🛡 Профилактика", "👨‍⚕️ Записаться на консультацию"],
        ],
        resize_keyboard=True,
    )

    original_message = bot.message

    async def guide_start(update, context):
        context.user_data.clear()
        from storage import set_bot_setting
        await asyncio.to_thread(set_bot_setting, f"dialog_scope:{update.effective_user.id}", "general")
        await bot.ensure_current_user(update)
        try:
            from conversation_store import clear_conversation_history

            await asyncio.to_thread(
                clear_conversation_history,
                update.effective_user.id,
            )
        except Exception:
            pass

        await update.message.reply_text(START_TEXT, reply_markup=bot.MENU)

    async def guide_message(update, context):
        text = (update.message.text or "").strip() if update.message else ""
        # Старый текстовый триггер оставляем для совместимости, но кнопку из меню убираем.
        if text == "ℹ️ Возможности":
            await update.message.reply_text(CAPABILITIES_TEXT, reply_markup=bot.MENU)
            return
        return await original_message(update, context)

    bot.start = guide_start
    bot.message = guide_message
