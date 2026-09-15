import asyncio
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from storage import get_bot_setting, set_bot_setting


FALLBACK_ADMIN_TELEGRAM_ID = 754526258


def _admin_user_id() -> int:
    raw = (os.getenv("ADMIN_TELEGRAM_ID") or "").strip()
    try:
        return int(raw) if raw else FALLBACK_ADMIN_TELEGRAM_ID
    except ValueError:
        return FALLBACK_ADMIN_TELEGRAM_ID


def _admin_chat_id() -> int:
    saved = get_bot_setting("admin_notification_chat_id")
    if saved:
        try:
            return int(saved)
        except ValueError:
            pass
    return _admin_user_id()


def install(bot):
    original_callback = bot.consult_callback

    async def set_admin_chat_command(update, context):
        user = update.effective_user
        chat = update.effective_chat

        if not user or user.id != _admin_user_id():
            await update.message.reply_text("Эта команда доступна только администратору бота.")
            return

        if not chat or chat.type not in {"group", "supergroup"}:
            await update.message.reply_text(
                "Добавьте бота в отдельную приватную группу «МойДоктор — заявки» "
                "и отправьте /setadminchat уже внутри этой группы."
            )
            return

        await asyncio.to_thread(
            set_bot_setting,
            "admin_notification_chat_id",
            str(chat.id),
        )
        await update.message.reply_text(
            "✅ Эта группа назначена для заявок.\n\n"
            "Теперь новые записи на консультацию будут приходить сюда."
        )

    async def reliable_consult_callback(update, context):
        query = update.callback_query
        data_value = query.data or ""

        if not data_value.startswith("consult:format:"):
            return await original_callback(update, context)

        await query.answer()
        await bot.ensure_current_user(update)

        flow = context.user_data.get("consult_flow")
        if not flow:
            await query.edit_message_text(
                "Анкета уже закрыта. Нажмите запись на консультацию ещё раз."
            )
            return

        data = flow.get("data", {})
        value = data_value.rsplit(":", 1)[1]
        labels = {
            "video": "Видеосвязь",
            "voice": "Голосовые сообщения",
            "chat": "Чат",
        }
        format_name = labels.get(value, "Чат")
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
        target_chat_id = await asyncio.to_thread(_admin_chat_id)
        try:
            await context.bot.send_message(
                chat_id=target_chat_id,
                text=summary,
            )
            delivered = True

            if user.username:
                try:
                    await context.bot.send_message(
                        chat_id=target_chat_id,
                        text="Связаться с владельцем:",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton(
                                "💬 Написать владельцу",
                                url=f"https://t.me/{user.username}",
                            )]]
                        ),
                    )
                except Exception as button_error:
                    print(f"consult contact button error: {button_error!r}", flush=True)
        except Exception as exc:
            print(
                f"consult notification error admin_chat={target_chat_id}: {exc!r}",
                flush=True,
            )

        context.user_data.pop("consult_flow", None)
        try:
            await query.edit_message_text(f"Формат: {format_name}")
        except Exception:
            pass

        if delivered:
            await query.message.reply_text(
                "✅ Заявка отправлена Ивану Андреевичу.\n\n"
                "Он получил вашу анкету и сможет связаться с вами в Telegram.",
                reply_markup=bot.MENU,
            )
        else:
            await query.message.reply_text(
                "✅ Анкета сохранена.\n\n"
                "Не удалось отправить уведомление врачу автоматически. "
                "Заявка сохранена в системе.",
                reply_markup=bot.MENU,
            )

    bot.consult_callback = reliable_consult_callback
    bot.set_admin_chat_command = set_admin_chat_command
