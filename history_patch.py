import asyncio
import io

from conversation_store import (
    clear_conversation_history,
    init_conversation_store,
    load_conversation_history,
    save_conversation_message,
)


PERSISTENT_CONTEXT_RULES = """

НЕПРЕРЫВНЫЙ КОНТЕКСТ ДИАЛОГА
- Продолжай текущий клинический случай по предыдущим сообщениям независимо от того, были они текстом, голосовым или разбором изображения.
- Выбранный в приложении питомец — только сохранённый профиль, а не автоматическое указание, что любой текущий вопрос относится к нему.
- Если текущая переписка или документ явно относится к другому животному, не переключай разговор на активного питомца.
- Короткие продолжения вроде «предварительный диагноз?», «а дальше?», «почему?», «?» трактуй в контексте предыдущего сообщения, а не как начало нового приёма.
"""


def install(bot):
    init_conversation_store()
    if PERSISTENT_CONTEXT_RULES not in bot.SYSTEM_PROMPT:
        bot.SYSTEM_PROMPT += PERSISTENT_CONTEXT_RULES

    original_start = bot.start
    original_ask_ai = bot.ask_ai
    original_voice_message = bot.voice_message
    original_media = bot.media

    async def _hydrate(update, context):
        if "dialog_scope" not in context.user_data:
            from storage import get_bot_setting
            scope = await asyncio.to_thread(get_bot_setting, f"dialog_scope:{update.effective_user.id}")
            if scope in {"pet", "general"}:
                context.user_data["dialog_scope"] = scope
        history = context.user_data.setdefault("history", [])
        if history:
            return history
        restored = await asyncio.to_thread(
            load_conversation_history,
            update.effective_user.id,
            12,
        )
        if restored:
            history.extend(restored)
        if context.user_data.get("dialog_scope") == "general":
            from onboarding_patch import GENERAL_MARKER
            history.insert(0, {"role": "user", "content": GENERAL_MARKER})
        return history

    async def persistent_start(update, context):
        if update.effective_user:
            await asyncio.to_thread(
                clear_conversation_history,
                update.effective_user.id,
            )
        return await original_start(update, context)

    async def persistent_ask_ai(update, context):
        await _hydrate(update, context)
        user_text = (update.message.text or "").strip() if update.message else ""
        if user_text:
            await asyncio.to_thread(
                save_conversation_message,
                update.effective_user.id,
                "user",
                user_text,
                "text",
            )

        before = list(context.user_data.get("history", []))
        await original_ask_ai(update, context)
        history = context.user_data.get("history", [])
        if history:
            for item in reversed(history):
                if any(item is old for old in before):
                    break
                if item.get("role") == "assistant" and item.get("content"):
                    await asyncio.to_thread(
                        save_conversation_message,
                        update.effective_user.id,
                        "assistant",
                        str(item["content"]),
                        "text",
                    )
                    break

    async def persistent_voice_message(update, context):
        # Booking-form voice messages keep their existing dedicated flow.
        if context.user_data.get("consult_flow"):
            return await original_voice_message(update, context)

        await bot.ensure_current_user(update)
        if bot.client is None:
            await update.message.reply_text("ИИ временно не подключён.", reply_markup=bot.MENU)
            return

        voice = update.message.voice
        if not voice:
            return
        if (voice.file_size or 0) > 20 * 1024 * 1024:
            await update.message.reply_text(
                "Голосовое слишком большое. Пришлите более короткое сообщение.",
                reply_markup=bot.MENU,
            )
            return

        await update.message.chat.send_action("typing")
        try:
            tg_file = await context.bot.get_file(voice.file_id)
            audio_bytes = bytes(await tg_file.download_as_bytearray())
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "voice.ogg"
            transcription = await asyncio.to_thread(
                bot.client.audio.transcriptions.create,
                model="gpt-4o-mini-transcribe",
                file=audio_file,
                language="ru",
            )
            user_text = (transcription.text or "").strip()
            if not user_text:
                raise ValueError("empty transcription")
            print(
                f"voice transcribed user={update.effective_user.id} chars={len(user_text)}",
                flush=True,
            )
        except Exception as exc:
            print(f"voice transcription error: {exc!r}", flush=True)
            await update.message.reply_text(
                "Не получилось распознать голосовое. Попробуйте записать ещё раз.",
                reply_markup=bot.MENU,
            )
            return

        history = await _hydrate(update, context)
        await asyncio.to_thread(
            save_conversation_message,
            update.effective_user.id,
            "user",
            user_text,
            "voice",
        )

        if context.user_data.pop("diagnostics_mode", False):
            history.append(
                {
                    "role": "user",
                    "content": "[DIAGNOSTICS_MODE] Пользователь выбирает или сравнивает анализы/обследования.",
                }
            )
        history.append({"role": "user", "content": user_text})
        history[:] = history[-12:]

        pet = await asyncio.to_thread(bot.get_active_pet, update.effective_user.id)
        instructions = bot.SYSTEM_PROMPT + "\n\nДанные активного питомца:\n" + bot.pet_summary(pet)

        try:
            response = await asyncio.to_thread(
                bot.client.responses.create,
                model="gpt-5.6-sol",
                instructions=instructions,
                input=history,
            )
            answer = (response.output_text or "").strip()
            if not answer:
                answer = "Не удалось сформировать ответ. Пожалуйста, повторите сообщение."
            history.append({"role": "assistant", "content": answer})
            history[:] = history[-12:]
            await asyncio.to_thread(
                save_conversation_message,
                update.effective_user.id,
                "assistant",
                answer,
                "voice",
            )
            await asyncio.to_thread(
                bot.save_consultation,
                update.effective_user.id,
                user_text,
                answer,
                "voice_chat",
            )
        except Exception as exc:
            print(f"voice ai response error: {exc!r}", flush=True)
            await update.message.reply_text(
                "Голосовое распознано и сохранено, но сейчас не удалось сформировать ответ. "
                "Можно отправить следующее сообщение — контекст не потеряется.",
                reply_markup=bot.MENU,
            )
            return

        await update.message.reply_text(answer, reply_markup=bot.MENU)

    async def persistent_media(update, context):
        await _hydrate(update, context)
        caption = (update.message.caption or "").strip() if update.message else ""
        marker = "Пользователь загрузил медицинский документ/изображение."
        if caption:
            marker += f" Запрос: {caption}"
        await asyncio.to_thread(
            save_conversation_message,
            update.effective_user.id,
            "user",
            marker,
            "media",
        )

        before = list(context.user_data.get("history", []))
        result = await original_media(update, context)
        history = context.user_data.get("history", [])
        if history:
            for item in reversed(history):
                if any(item is old for old in before):
                    break
                if item.get("role") == "assistant" and item.get("content"):
                    await asyncio.to_thread(
                        save_conversation_message,
                        update.effective_user.id,
                        "assistant",
                        str(item["content"]),
                        "media",
                    )
                    break
        return result

    bot.start = persistent_start
    bot.ask_ai = persistent_ask_ai
    bot.voice_message = persistent_voice_message
    bot.media = persistent_media
