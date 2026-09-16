import asyncio
from contextlib import suppress

from telegram.constants import ChatAction


async def _typing_loop(update):
    message = getattr(update, "effective_message", None)
    if message is None:
        return

    while True:
        try:
            await message.chat.send_action(ChatAction.TYPING)
        except Exception:
            return
        await asyncio.sleep(4)


async def _run_with_typing(update, handler, *args, **kwargs):
    task = asyncio.create_task(_typing_loop(update))
    try:
        return await handler(update, *args, **kwargs)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def install(bot):
    original_message = bot.message
    original_voice_message = bot.voice_message
    original_media = bot.media

    async def message_with_typing(update, context):
        text = (getattr(update.effective_message, "text", None) or "").strip()

        # Menu navigation normally answers instantly and does not need a long-running indicator.
        quick_actions = {
            "🐾 Мои питомцы",
            "➕ Добавить питомца",
            "💬 Задать вопрос",
            "🧪 Анализы и документы",
            "📋 История обращений",
            "👨‍⚕️ Записаться на консультацию",
            "🌐 Общий вопрос",
            "🐾 Выбрать питомца",
            "🐾 Привязать к питомцу",
            "🌐 Без привязки",
            "❌ Отмена",
        }
        if text in quick_actions:
            return await original_message(update, context)

        return await _run_with_typing(update, original_message, context)

    async def voice_with_typing(update, context):
        return await _run_with_typing(update, original_voice_message, context)

    async def media_with_typing(update, context):
        return await _run_with_typing(update, original_media, context)

    bot.message = message_with_typing
    bot.voice_message = voice_with_typing
    bot.media = media_with_typing
