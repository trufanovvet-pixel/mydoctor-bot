from patient_context import MAX_MESSAGES, PATIENT_FACT_RULES
import asyncio
import io
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import app as base_app


ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")


def _cancel_keyboard(bot):
    return ReplyKeyboardMarkup(
        [["❌ Отмена"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def install_features(bot):
    original_message = bot.message

    async def show_consultation(update, context):
        await bot.ensure_current_user(update)
        await update.message.reply_text(
            "Онлайн-консультация с Иваном Труфановым.\n\n"
            "Первичная — 3000 ₽ / $35.\n"
            "Повторная — 1500 ₽ / $20.\n\n"
            "Запись доступна без привязки к определённым дням недели. "
            "Нажмите «Записаться» — я открою короткую анкету.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📝 Записаться", callback_data="consult:start")]]
            ),
        )

    async def ask_complaint_from_callback(query, context):
        flow = context.user_data["consult_flow"]
        flow["step"] = "complaint"
        await query.message.reply_text(
            "Опишите основную проблему: что беспокоит питомца и как давно?\n\n"
            "Можно написать текстом или отправить голосовое.",
            reply_markup=_cancel_keyboard(bot),
        )

    async def submit_consultation(update, context, format_name):
        flow = context.user_data.get("consult_flow")
        if not flow:
            return
        data = flow["data"]
        data["format"] = format_name
        user = update.effective_user

        kind_label = data.get("kind_label", "Не указано")
        contact = f"@{user.username}" if user.username else f"Telegram ID: {user.id}"
        summary = (
            "🩺 НОВАЯ ЗАПИСЬ НА КОНСУЛЬТАЦИЮ\n\n"
            f"Тип: {kind_label}\n"
            f"Владелец: {data.get('owner_name', 'Не указано')}\n"
            f"Telegram: {contact}\n"
            f"Питомец: {data.get('pet', 'Не указано')}\n\n"
            f"Запрос: {data.get('complaint', 'Не указано')}\n\n"
            f"Обследования / лечение: {data.get('medical', 'Не указано')}\n\n"
            f"Предпочтительный формат: {format_name}"
        )

        await asyncio.to_thread(
            bot.save_consultation,
            user.id,
            summary,
            "Заявка на онлайн-консультацию",
            "consult_request",
        )

        delivered = False
        if ADMIN_TELEGRAM_ID:
            try:
                profile_url = (
                    f"https://t.me/{user.username}"
                    if user.username
                    else f"tg://user?id={user.id}"
                )
                await context.bot.send_message(
                    chat_id=int(ADMIN_TELEGRAM_ID),
                    text=summary,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("💬 Написать владельцу", url=profile_url)]]
                    ),
                )
                delivered = True
            except Exception:
                delivered = False

        context.user_data.pop("consult_flow", None)
        query = update.callback_query
        if query:
            try:
                await query.edit_message_text(f"Формат: {format_name}")
            except Exception:
                pass

        if delivered:
            text = (
                "✅ Заявка отправлена Ивану Андреевичу.\n\n"
                "Он увидит вашу анкету и сможет связаться с вами напрямую в Telegram."
            )
        else:
            text = (
                "✅ Анкета сохранена.\n\n"
                "Уведомление врачу сейчас не подключено, поэтому запись пока сохранена в системе."
            )
        await query.message.reply_text(text, reply_markup=bot.MENU)

    async def consult_callback(update, context):
        query = update.callback_query
        await query.answer()
        await bot.ensure_current_user(update)
        data = query.data or ""

        if data == "consult:cancel":
            context.user_data.pop("consult_flow", None)
            await query.edit_message_text("Запись отменена.")
            await query.message.reply_text("Выберите нужный раздел 👇", reply_markup=bot.MENU)
            return

        if data == "consult:start":
            context.user_data["consult_flow"] = {"step": "type", "data": {}}
            await query.edit_message_text(
                "Выберите тип консультации:",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Первичная — 3000 ₽ / $35", callback_data="consult:type:primary")],
                        [InlineKeyboardButton("Повторная — 1500 ₽ / $20", callback_data="consult:type:repeat")],
                        [InlineKeyboardButton("❌ Отмена", callback_data="consult:cancel")],
                    ]
                ),
            )
            return

        flow = context.user_data.get("consult_flow")
        if not flow:
            await query.edit_message_text("Анкета уже закрыта. Нажмите запись на консультацию ещё раз.")
            return

        if data.startswith("consult:type:"):
            selected = data.rsplit(":", 1)[1]
            if selected == "primary":
                flow["data"]["kind"] = "primary"
                flow["data"]["kind_label"] = "Первичная — 3000 ₽ / $35"
            else:
                flow["data"]["kind"] = "repeat"
                flow["data"]["kind_label"] = "Повторная — 1500 ₽ / $20"
            flow["step"] = "owner_name"
            await query.edit_message_text(flow["data"]["kind_label"])
            await query.message.reply_text(
                "Как вас зовут?",
                reply_markup=_cancel_keyboard(bot),
            )
            return

        if data == "consult:pet:active":
            pet = await asyncio.to_thread(bot.get_active_pet, update.effective_user.id)
            if pet:
                flow["data"]["pet"] = bot.pet_summary(pet)
                await query.edit_message_text(f"Питомец: {pet['name']}")
                await ask_complaint_from_callback(query, context)
            else:
                flow["step"] = "pet"
                await query.edit_message_text("Сохранённый питомец не найден.")
                await query.message.reply_text(
                    "Напишите одним сообщением: имя, вид, порода, возраст и вес питомца.",
                    reply_markup=_cancel_keyboard(bot),
                )
            return

        if data == "consult:pet:other":
            flow["step"] = "pet"
            await query.edit_message_text("Другой питомец")
            await query.message.reply_text(
                "Напишите одним сообщением: имя, вид, порода, возраст и вес питомца.\n\n"
                "Можно отправить голосовое.",
                reply_markup=_cancel_keyboard(bot),
            )
            return

        if data.startswith("consult:format:"):
            value = data.rsplit(":", 1)[1]
            labels = {
                "video": "Видеосвязь",
                "voice": "Голосовые сообщения",
                "chat": "Чат",
            }
            await submit_consultation(update, context, labels.get(value, "Чат"))

    async def handle_consult_text(update, context, text):
        flow = context.user_data.get("consult_flow")
        if not flow:
            return False

        text = (text or "").strip()
        if not text:
            return True

        if text == "❌ Отмена":
            context.user_data.pop("consult_flow", None)
            await update.message.reply_text("Запись отменена.", reply_markup=bot.MENU)
            return True

        step = flow["step"]
        data = flow["data"]

        if step == "owner_name":
            data["owner_name"] = text[:150]
            active = await asyncio.to_thread(bot.get_active_pet, update.effective_user.id)
            if active:
                flow["step"] = "pet_choice"
                await update.message.reply_text(
                    f"Записываем питомца {active['name']}?",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton(f"Да, {active['name']}", callback_data="consult:pet:active")],
                            [InlineKeyboardButton("Другой питомец", callback_data="consult:pet:other")],
                            [InlineKeyboardButton("❌ Отмена", callback_data="consult:cancel")],
                        ]
                    ),
                )
            else:
                flow["step"] = "pet"
                await update.message.reply_text(
                    "Напишите одним сообщением: имя, вид, порода, возраст и вес питомца.\n\n"
                    "Можно отправить голосовое.",
                    reply_markup=_cancel_keyboard(bot),
                )
            return True

        if step == "pet":
            data["pet"] = text[:700]
            flow["step"] = "complaint"
            await update.message.reply_text(
                "Опишите основную проблему: что беспокоит питомца и как давно?\n\n"
                "Можно написать текстом или отправить голосовое.",
                reply_markup=_cancel_keyboard(bot),
            )
            return True

        if step == "complaint":
            data["complaint"] = text[:2500]
            flow["step"] = "medical"
            await update.message.reply_text(
                "Какие обследования или анализы уже сделаны и какое лечение / препараты получает питомец?\n\n"
                "Если ничего — напишите «нет». Можно ответить голосовым.",
                reply_markup=_cancel_keyboard(bot),
            )
            return True

        if step == "medical":
            data["medical"] = text[:2500]
            flow["step"] = "format"
            await update.message.reply_text(
                "Как удобнее провести консультацию?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("📹 Видеосвязь", callback_data="consult:format:video")],
                        [InlineKeyboardButton("🎙 Голосовые", callback_data="consult:format:voice")],
                        [InlineKeyboardButton("💬 Чат", callback_data="consult:format:chat")],
                        [InlineKeyboardButton("❌ Отмена", callback_data="consult:cancel")],
                    ]
                ),
            )
            return True

        return True

    async def ask_ai_text(update, context, user_text):
        history = context.user_data.setdefault("history", [])
        if context.user_data.pop("diagnostics_mode", False):
            history.append(
                {
                    "role": "user",
                    "content": "[DIAGNOSTICS_MODE] Пользователь выбирает или сравнивает анализы/обследования.",
                }
            )
        history.append({"role": "user", "content": user_text})
        history[:] = history[-MAX_MESSAGES:]
        await update.message.chat.send_action("typing")

        pet = await asyncio.to_thread(bot.get_active_pet, update.effective_user.id)
        instructions = bot.SYSTEM_PROMPT + "\n\nДанные активного питомца:\n" + bot.pet_summary(pet)

        try:
            from telegram_billing import generate
            response = await generate(update, bot.client,
                model="gpt-5.6-sol",
                instructions=instructions,
                input=history,
            )
            if response is None:
                return
            answer = response.output_text.strip()
            if not answer:
                answer = "Не удалось сформировать ответ. Пожалуйста, повторите сообщение."
            history.append({"role": "assistant", "content": answer})
            history[:] = history[-MAX_MESSAGES:]
            await asyncio.to_thread(
                bot.save_consultation,
                update.effective_user.id,
                user_text,
                answer,
                "voice_chat",
            )
        except Exception:
            answer = "Не удалось обработать голосовое. Попробуйте ещё раз или напишите сообщение текстом."

        await update.message.reply_text(answer, reply_markup=bot.MENU)

    async def voice_message(update, context):
        await bot.ensure_current_user(update)
        if bot.client is None:
            await update.message.reply_text("ИИ временно не подключён.", reply_markup=bot.MENU)
            return

        voice = update.message.voice
        if not voice:
            return
        if (voice.file_size or 0) > 20 * 1024 * 1024:
            await update.message.reply_text("Голосовое слишком большое. Пришлите более короткое сообщение.")
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
            text = (transcription.text or "").strip()
            if not text:
                raise ValueError("empty transcription")
        except Exception:
            await update.message.reply_text(
                "Не получилось распознать голосовое. Попробуйте записать ещё раз.",
                reply_markup=bot.MENU,
            )
            return

        if await handle_consult_text(update, context, text):
            return
        await ask_ai_text(update, context, text)

    async def myid_command(update, context):
        await bot.ensure_current_user(update)
        await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")

    async def enhanced_message(update, context):
        await bot.ensure_current_user(update)
        text = update.message.text or ""

        if await handle_consult_text(update, context, text):
            return
        if text == "👨‍⚕️ Записаться на консультацию":
            await show_consultation(update, context)
            return
        await original_message(update, context)

    bot.message = enhanced_message
    bot.consult_callback = consult_callback
    bot.voice_message = voice_message
    bot.myid_command = myid_command


def main():
    bot = base_app._load_bot_module()
    base_app._install_diagnostics_ui(bot)
    if bot.client is not None:
        bot.client = base_app._ClientWithKnowledge(bot.client)
    install_features(bot)

    if not bot.TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    bot.init_db()
    application = Application.builder().token(bot.TOKEN).build()
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("menu", bot.menu_command))
    application.add_handler(CommandHandler("myid", bot.myid_command))
    application.add_handler(CallbackQueryHandler(bot.pet_callback, pattern=r"^pet:\d+$"))
    application.add_handler(CallbackQueryHandler(bot.consult_callback, pattern=r"^consult:"))
    application.add_handler(MessageHandler(filters.VOICE, bot.voice_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.message))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, bot.media))
    application.run_polling()


if __name__ == "__main__":
    main()
