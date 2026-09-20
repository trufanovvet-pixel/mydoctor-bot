from patient_context import MAX_MESSAGES, PATIENT_FACT_RULES
import asyncio
import re

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup


GENERAL_MARKER = "[GENERAL_SCOPE]"
PET_MARKER = "[PET_SCOPE]"

GENERAL_SCOPE_RULES = """

РЕЖИМ ОБЩЕГО ВОПРОСА
Пользователь явно выбрал общий вопрос, не связанный с сохранённым питомцем.
- Не используй данные активного питомца, его имя, породу, возраст, вес или историю.
- Не превращай справочный вопрос в консультацию по активному питомцу.
- Отвечай на сам вопрос; уточняй только то, без чего действительно нельзя ответить.
"""


def _strip_active_pet_context(instructions: str) -> str:
    return re.sub(
        r"\n\nДанные активного питомца:\n.*$",
        "",
        instructions or "",
        flags=re.DOTALL,
    )


def _scope_from_input(response_input):
    if not isinstance(response_input, list):
        return None, response_input

    last_index = None
    scope = None
    for index, message in enumerate(response_input):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if content == GENERAL_MARKER:
            last_index = index
            scope = "general"
        elif content == PET_MARKER:
            last_index = index
            scope = "pet"

    if last_index is None:
        return None, response_input
    return scope, response_input[last_index + 1 :]


class _ScopedResponses:
    def __init__(self, base_client):
        self._base_client = base_client

    def create(self, *args, **kwargs):
        scope, cleaned_input = _scope_from_input(kwargs.get("input"))
        if scope:
            kwargs["input"] = cleaned_input
        if scope == "general":
            kwargs["instructions"] = (
                _strip_active_pet_context(kwargs.get("instructions") or "")
                + GENERAL_SCOPE_RULES
            )
        return self._base_client.responses.create(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._base_client.responses, name)


class _ScopedClient:
    def __init__(self, base_client):
        self._base_client = base_client
        self.responses = _ScopedResponses(base_client)

    def __getattr__(self, name):
        return getattr(self._base_client, name)


def _scope_marker(scope: str) -> str:
    return GENERAL_MARKER if scope == "general" else PET_MARKER


def _set_dialog_scope(context, scope: str):
    context.user_data["dialog_scope"] = scope
    context.user_data["history"] = [
        {"role": "user", "content": _scope_marker(scope)}
    ]


async def _remember_scope(user_id, scope):
    from storage import set_bot_setting
    from conversation_store import clear_conversation_history
    await asyncio.to_thread(set_bot_setting, f"dialog_scope:{user_id}", scope)
    await asyncio.to_thread(clear_conversation_history, user_id)


def _prepare_scope_history(context):
    scope = context.user_data.get("dialog_scope")
    if scope not in {"general", "pet"}:
        return

    history = context.user_data.setdefault("history", [])
    cleaned = [
        item
        for item in history
        if not (
            isinstance(item, dict)
            and isinstance(item.get("content"), str)
            and item.get("content") in {GENERAL_MARKER, PET_MARKER}
        )
    ]
    cleaned = cleaned[-(MAX_MESSAGES - 2):]
    history[:] = [
        {"role": "user", "content": _scope_marker(scope)},
        *cleaned,
    ]


def _reset_choice_state(context):
    context.user_data.pop("choice_flow", None)
    context.user_data.pop("after_pet_action", None)


def _choice_keyboard(rows):
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def _pet_buttons(pets):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(pet["name"], callback_data=f"pet:{pet['id']}")]
            for pet in pets
        ]
    )


def _load_pet_history(telegram_id: int, pet_id: int, limit: int = 8):
    import storage

    with storage.SessionLocal() as session:
        user = session.scalar(
            select(storage.User).where(storage.User.telegram_id == telegram_id)
        )
        if user is None:
            return []
        rows = session.scalars(
            select(storage.Consultation)
            .where(
                storage.Consultation.user_id == user.id,
                storage.Consultation.pet_id == pet_id,
            )
            .order_by(storage.Consultation.created_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "created_at": row.created_at,
                "kind": row.kind,
                "user_text": row.user_text,
            }
            for row in rows
        ]


def _history_text(pet, items):
    if not items:
        return f"По питомцу {pet['name']} пока нет сохранённых обращений."

    lines = [f"📋 Последние обращения: {pet['name']}", ""]
    for item in items:
        created = item["created_at"].strftime("%d.%m.%Y") if item.get("created_at") else "—"
        text = " ".join((item.get("user_text") or "").split())
        if len(text) > 110:
            text = text[:107] + "…"
        lines.append(f"{created} — {text or 'обращение'}")
    return "\n".join(lines)


def install(bot):
    bot.MENU = ReplyKeyboardMarkup(
        [
            ["🐾 Мои питомцы", "➕ Добавить питомца"],
            ["💬 Задать вопрос", "🧪 Анализы и документы"],
            ["📋 История обращений", "👨‍⚕️ Записаться на консультацию"],
        ],
        resize_keyboard=True,
    )

    original_message = bot.message
    original_start = bot.start
    original_pet_callback = bot.pet_callback
    original_voice_message = bot.voice_message
    original_media = bot.media

    if bot.client is not None:
        bot.client = _ScopedClient(bot.client)

    async def guided_start(update, context):
        context.user_data.clear()
        await bot.ensure_current_user(update)
        try:
            from conversation_store import clear_conversation_history

            await asyncio.to_thread(
                clear_conversation_history,
                update.effective_user.id,
            )
        except Exception:
            pass

        await update.message.reply_text(
            "🐾 Привет! Я ветеринарный помощник.\n\n"
            "Могу помочь разобраться в симптомах, анализах, обследованиях и назначениях, "
            "а также хранить карточки питомцев и историю обращений.\n\n"
            "Если вопрос общий — питомца выбирать не нужно. Если вопрос касается конкретного "
            "животного — выберите его перед началом разговора.\n\n"
            "Можно писать текстом, отправлять голосовые, фотографии и PDF.\n\n"
            "С чего начнём?",
            reply_markup=bot.MENU,
        )

    async def _show_pet_picker(update, context, action: str):
        await bot.ensure_current_user(update)
        pets = await asyncio.to_thread(bot.list_pets, update.effective_user.id)
        if not pets:
            context.user_data["adding_pet"] = {"step": "name"}
            _reset_choice_state(context)
            await update.message.reply_text(
                "Сначала добавим питомца. Как его зовут?",
                reply_markup=_choice_keyboard([["❌ Отмена"]]),
            )
            return

        context.user_data["after_pet_action"] = action
        await update.message.reply_text(
            "Выберите питомца:",
            reply_markup=_pet_buttons(pets),
        )

    async def _show_active_pet_history(message, user_id: int):
        pet = await asyncio.to_thread(bot.get_active_pet, user_id)
        if not pet:
            await message.reply_text("Сначала выберите питомца.", reply_markup=bot.MENU)
            return
        items = await asyncio.to_thread(_load_pet_history, user_id, pet["id"])
        await message.reply_text(_history_text(pet, items), reply_markup=bot.MENU)

    async def scoped_pet_callback(update, context):
        action = context.user_data.pop("after_pet_action", None)
        if await original_pet_callback(update, context) is False:
            return False
        from conversation_store import clear_conversation_history
        await asyncio.to_thread(clear_conversation_history, update.effective_user.id)
        context.user_data.pop("records_target_explicit", None)
        context.user_data.pop("records_target_pet_id", None)
        pet = await asyncio.to_thread(bot.get_active_pet, update.effective_user.id)
        if not pet:
            return

        if action != "history":
            await _remember_scope(update.effective_user.id, "pet")

        if action == "question":
            _set_dialog_scope(context, "pet")
            await update.callback_query.message.reply_text(
                f"Хорошо, вопрос будет про {pet['name']}.\n"
                "Опишите ситуацию своими словами или отправьте голосовое.",
                reply_markup=bot.MENU,
            )
        elif action == "media":
            _set_dialog_scope(context, "pet")
            await update.callback_query.message.reply_text(
                f"Документы будут относиться к питомцу {pet['name']}.\n"
                "Пришлите фото или PDF и при желании добавьте вопрос в подписи.",
                reply_markup=bot.MENU,
            )
        elif action == "history":
            await _show_active_pet_history(
                update.callback_query.message,
                update.effective_user.id,
            )
        else:
            _set_dialog_scope(context, "pet")

    async def guided_message(update, context):
        if context.user_data.get("adding_pet") or context.user_data.get("consult_flow"):
            return await original_message(update, context)

        text = (update.message.text or "").strip()

        if text == "💬 Задать вопрос":
            context.user_data["choice_flow"] = "question"
            await update.message.reply_text(
                "Вопрос общий или касается конкретного питомца?",
                reply_markup=_choice_keyboard(
                    [["🌐 Общий вопрос", "🐾 Выбрать питомца"], ["❌ Отмена"]]
                ),
            )
            return

        if text == "🌐 Общий вопрос" and context.user_data.get("choice_flow") == "question":
            _reset_choice_state(context)
            _set_dialog_scope(context, "general")
            await _remember_scope(update.effective_user.id, "general")
            await update.message.reply_text(
                "Хорошо. Напишите общий вопрос или отправьте голосовое. "
                "Данные сохранённых питомцев использоваться не будут.",
                reply_markup=bot.MENU,
            )
            return

        if text == "🐾 Выбрать питомца" and context.user_data.get("choice_flow") == "question":
            _reset_choice_state(context)
            await _show_pet_picker(update, context, "question")
            return

        if text == "🧪 Анализы и документы":
            context.user_data["choice_flow"] = "media"
            await update.message.reply_text(
                "Кому принадлежат анализы или документы?",
                reply_markup=_choice_keyboard(
                    [["🐾 Привязать к питомцу", "🌐 Без привязки"], ["❌ Отмена"]]
                ),
            )
            return

        if text == "🐾 Привязать к питомцу" and context.user_data.get("choice_flow") == "media":
            _reset_choice_state(context)
            await _show_pet_picker(update, context, "media")
            return

        if text == "🌐 Без привязки" and context.user_data.get("choice_flow") == "media":
            _reset_choice_state(context)
            _set_dialog_scope(context, "general")
            await _remember_scope(update.effective_user.id, "general")
            await update.message.reply_text(
                "Пришлите фото или PDF. Документ разберу без использования данных сохранённого питомца.",
                reply_markup=bot.MENU,
            )
            return

        if text == "📋 История обращений":
            await _show_pet_picker(update, context, "history")
            return

        if text == "❌ Отмена" and context.user_data.get("choice_flow"):
            _reset_choice_state(context)
            await update.message.reply_text("Отменено.", reply_markup=bot.MENU)
            return

        if text in {
            "🐾 Мои питомцы",
            "➕ Добавить питомца",
            "👨‍⚕️ Записаться на консультацию",
        }:
            return await original_message(update, context)

        _prepare_scope_history(context)
        return await original_message(update, context)

    async def scoped_voice_message(update, context):
        if not context.user_data.get("consult_flow"):
            _prepare_scope_history(context)
        return await original_voice_message(update, context)

    async def scoped_media(update, context):
        _prepare_scope_history(context)
        return await original_media(update, context)

    bot.start = guided_start
    bot.message = guided_message
    bot.pet_callback = scoped_pet_callback
    bot.voice_message = scoped_voice_message
    bot.media = scoped_media
