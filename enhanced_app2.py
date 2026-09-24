import re
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import app as base_app
import enhanced_app as enhanced
from knowledge import protocol_context, diagnostics_context, is_diagnostics_query


REFERENCE_MODE = """

СПРАВОЧНЫЙ РЕЖИМ
Пользователь задал общий информационный вопрос, а не начал консультацию по конкретному пациенту.
Ответь сразу по существу и не превращай ответ в сбор анамнеза.

Правила:
- не спрашивай имя питомца, вид, породу, возраст, симптомы, анализы или диагноз, если пользователь сам не просит разобрать конкретный случай;
- не упоминай выбранного/активного питомца и не используй его историю;
- не задавай уточняющие вопросы в конце просто ради продолжения диалога;
- если спрашивают «что такое X» — кратко объясни, что это, у каких животных встречается, в чём суть заболевания/термина, основные характерные признаки и почему это важно;
- если вопрос про препарат — объясни, что это за препарат/группа и для чего применяется, без назначения конкретному животному;
- если спрашивают разницу между терминами/методами — сравни их напрямую;
- пиши простым профессиональным русским языком, компактно, обычно 5–10 предложений.
"""


def _latest_user_text(response_input) -> str:
    if not isinstance(response_input, list):
        return ""
    for message in reversed(response_input):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            if content.startswith("[DIAGNOSTICS_MODE]"):
                continue
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "input_text":
                    value = item.get("text")
                    if isinstance(value, str):
                        parts.append(value)
            if parts:
                return "\n".join(parts).strip()
    return ""


def _is_reference_question(text: str) -> bool:
    value = (text or "").strip().lower().replace("ё", "е")
    if not value:
        return False

    clinical_case_markers = (
        "что делать", "как лечить", "чем лечить", "какую доз", "сколько дать",
        "можно ли дать", "у моего", "у моей", "у нашего", "у нашей",
        "у него сейчас", "у нее сейчас", "моей собак", "моего кот", "моей кош",
    )
    if any(marker in value for marker in clinical_case_markers):
        return False

    patterns = (
        r"^(что такое)\b",
        r"^(что значит)\b",
        r"^(что означает)\b",
        r"^(что это(?: такое)?)\b",
        r"^(объясни(?:,| пожалуйста)?(?: что такое| что значит| термин)?)\b",
        r"^(расскажи(?:,| пожалуйста)? (?:про|о|об))\b",
        r"^(в чем разница)\b",
        r"^(чем отличается)\b",
        r"^(для чего (?:нужен|нужна|нужно))\b",
        r"^(зачем (?:нужен|нужна|нужно))\b",
        r"^как (?:проходит|проводят|делают|готовиться|готовят)\b",
        r"^(?:можно|можно ли) (?:удалить|убрать|прижечь|заморозить|провести)\b",
    )
    if any(re.search(pattern, value) for pattern in patterns):
        return True
    if re.search(r"\bэто что\??$", value):
        return True
    return False


def _strip_pet_context(instructions: str) -> str:
    return re.sub(
        r"\n\nДанные активного питомца:\n.*$",
        "",
        instructions or "",
        flags=re.DOTALL,
    )


class _ReferenceAwareResponses:
    def __init__(self, base_client):
        self._base_client = base_client

    def create(self, *args, **kwargs):
        latest_text = _latest_user_text(kwargs.get("input"))
        if base_app._has_media(kwargs.get("input")) or not _is_reference_question(latest_text):
            return self._base_client.responses.create(*args, **kwargs)

        instructions = _strip_pet_context(kwargs.get("instructions") or "") + REFERENCE_MODE
        instructions += protocol_context(latest_text)
        if is_diagnostics_query(latest_text):
            instructions += diagnostics_context()
        kwargs["instructions"] = instructions
        kwargs["input"] = [{"role": "user", "content": latest_text}]

        raw_client = getattr(self._base_client, "_client", None)
        if raw_client is not None:
            return raw_client.responses.create(*args, **kwargs)
        return self._base_client.responses.create(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._base_client.responses, name)


class _ReferenceAwareClient:
    def __init__(self, base_client):
        self._base_client = base_client
        self.responses = _ReferenceAwareResponses(base_client)

    def __getattr__(self, name):
        return getattr(self._base_client, name)


def install_clean_menu(bot):
    bot.MENU = ReplyKeyboardMarkup(
        [
            ["🩺 Описать симптомы", "📄 Загрузить анализы"],
            ["🧪 Анализы и обследования", "💊 Разобрать назначения"],
            ["🐾 Мои питомцы", "👨‍⚕️ Записаться на консультацию"],
        ],
        resize_keyboard=True,
    )

    original_message = bot.message

    async def show_pet_hub(update, context):
        await bot.ensure_current_user(update)
        pets = await __import__("asyncio").to_thread(bot.list_pets, update.effective_user.id)
        active = await __import__("asyncio").to_thread(bot.get_active_pet, update.effective_user.id)

        buttons = []
        if pets:
            lines = ["Ваши питомцы:"]
            for pet in pets:
                marker = "✅" if active and active["id"] == pet["id"] else "▫️"
                lines.append(f"{marker} {pet['name']} — {pet['species']}")
                buttons.append([
                    InlineKeyboardButton(
                        f"Выбрать: {pet['name']}",
                        callback_data=f"pet:{pet['id']}",
                    )
                ])
            text = "\n".join(lines) + "\n\nЗдесь можно выбрать питомца или добавить нового."
        else:
            text = "У вас пока нет сохранённых питомцев. Здесь можно добавить первого."

        buttons.append([InlineKeyboardButton("➕ Добавить питомца", callback_data="pets:add")])
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    async def clean_message(update, context):
        text = update.message.text if update.message else ""
        if text == "🐾 Мои питомцы":
            await show_pet_hub(update, context)
            return
        return await original_message(update, context)

    async def add_pet_callback(update, context):
        query = update.callback_query
        await query.answer()
        await bot.ensure_current_user(update)
        context.user_data["adding_pet"] = {"step": "name"}
        try:
            await query.edit_message_text("Добавление нового питомца")
        except Exception:
            pass
        await query.message.reply_text(
            "Как зовут питомца?",
            reply_markup=ReplyKeyboardMarkup(
                [["❌ Отмена"]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )

    bot.message = clean_message
    bot.add_pet_callback = add_pet_callback


def build_bot():
    bot = base_app._load_bot_module()
    base_app._install_diagnostics_ui(bot)
    if bot.client is not None:
        bot.client = base_app._ClientWithKnowledge(bot.client)
    enhanced.install_features(bot)
    install_clean_menu(bot)
    if bot.client is not None:
        bot.client = _ReferenceAwareClient(bot.client)
    return bot


async def handle_error(update, context):
    logging.getLogger(__name__).error("Telegram handler failure: %s", type(context.error).__name__)
    if update is not None and getattr(update, "effective_message", None):
        try:
            await update.effective_message.reply_text("Не удалось завершить запрос. Попробуйте ещё раз.")
        except Exception:
            logging.getLogger(__name__).warning("Unable to deliver error notice")


def main():
    bot = build_bot()

    if not bot.TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    bot.init_db()
    from consultation_delivery import start_delivery_worker, stop_delivery_worker
    application = (Application.builder().token(bot.TOKEN)
                   .post_init(start_delivery_worker).post_stop(stop_delivery_worker).build())
    application.add_error_handler(handle_error)
    from doctor_access import doctor_command,doctor_start_handler
    application.add_handler(CommandHandler("start", doctor_start_handler(bot.start)))
    application.add_handler(CommandHandler("menu", bot.menu_command))
    application.add_handler(CommandHandler("myid", bot.myid_command))
    application.add_handler(CommandHandler("doctor", doctor_command))
    if hasattr(bot, 'payment_command'):
        application.add_handler(CommandHandler(['pay', 'balance'], bot.payment_command))
    if hasattr(bot, "set_admin_chat_command"):
        application.add_handler(CommandHandler("setadminchat", bot.set_admin_chat_command))
    if hasattr(bot, "admin_chat_shared"):
        application.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, bot.admin_chat_shared))
    application.add_handler(CallbackQueryHandler(bot.add_pet_callback, pattern=r"^pets:add$"))
    application.add_handler(CallbackQueryHandler(bot.pet_callback, pattern=r"^pet:\d+$"))
    application.add_handler(CallbackQueryHandler(bot.consult_callback, pattern=r"^consult:"))
    application.add_handler(MessageHandler(filters.VOICE, bot.voice_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.message))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, bot.media))
    from knowledge import PROTOCOLS
    print(f"mydoctor startup: protocols={len(PROTOCOLS)} patch_chain=ready", flush=True)
    application.run_polling()


if __name__ == "__main__":
    main()
