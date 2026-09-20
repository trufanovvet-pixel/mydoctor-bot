from patient_context import PATIENT_FACT_RULES, controller_input
import json
import re
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

from knowledge import diagnostics_context, is_diagnostics_query, protocol_context


CLINICAL_REASONING_MODE = """

РЕЖИМ КЛИНИЧЕСКОГО ПРИЁМА
Веди диалог как обычный ветеринарный врач на первичном приёме.
Не пытайся за одно сообщение одновременно собрать анамнез, поставить диагноз,
назначить обследование и дать памятку.

Если текущая стадия INTERVIEW:
- одна короткая вводная фраза и 2–4 конкретных вопроса;
- не пиши диагнозы, обследования, лечение, памятку и оценку срочности без явного красного флага;
- не повторяй уже известные данные.

Если текущая стадия ASSESSMENT:
- дай предварительную оценку/локализацию;
- назови 2–4 наиболее вероятные причины;
- дай безопасные рекомендации на сейчас;
- предложи только оправданные исследования и объясни, зачем каждое нужно;
- не заменяй план одной фразой «обратитесь к ветеринару».

Если текущая стадия EMERGENCY:
- укажи конкретный уже описанный красный флаг;
- рекомендуй срочную очную помощь;
- не пугай редкими диагнозами без оснований.

Пиши живым профессиональным русским языком, как врач разговаривает с владельцем.
"""


DIAGNOSTICS_MODE = """

РЕЖИМ «АНАЛИЗЫ И ОБСЛЕДОВАНИЯ»
Это не первичный клинический приём. Не запускай обычный опрос по аппетиту, стулу и т.п.,
если он не нужен для выбора исследования.

Отвечай по структуре:
1. Что показывает каждый сравниваемый анализ/метод — простыми словами.
2. В какой ситуации один информативнее другого.
3. Главные ограничения и причины ложной/неоднозначной интерпретации.
4. Если без конкретной инфекции, срока болезни, вакцинации или материала нельзя выбрать
   ПЦР/ИФА/серологию — задай только эти уточняющие вопросы.
5. Не советуй сдавать всё подряд «для надёжности».
6. Если два исследования дополняют друг друга, объясни это прямо.
7. Не упоминай стоимость, бюджет, экономию, «что сдавать первым из-за денег» и подобные
   соображения, если пользователь сам явно не спросил про бюджет, цену или возможность
   сделать только часть исследований.
8. Данные выбранного в приложении питомца не относятся автоматически к общему вопросу.
   Не упоминай и не используй его имя, породу, возраст, вес или историю, если пользователь
   явно не спрашивает именно про этого питомца или не продолжает уже начатый разбор его случая.
"""


CLINICAL_CONTROLLER = """
Ты — скрытый клинический диспетчер ветеринарного приёма. Твой ответ не увидит владелец.
Проанализируй всю переписку по текущей жалобе и выбери одну стадию:
INTERVIEW, ASSESSMENT или EMERGENCY.

Не спеши к ASSESSMENT. Если остаются важные пробелы в характеристике симптома,
длительности/частоте/динамике, примесях, связанных симптомах, воде/аппетите,
возможной диетической погрешности, инородном теле, токсине, лекарствах или значимых
хронических болезнях — выбирай INTERVIEW и дай 2–4 следующих вопроса.
Нормально провести 2–3 круга вопросов.

ASSESSMENT — только когда данных достаточно для осмысленной первичной оценки.
EMERGENCY — только при уже описанном явном жизнеугрожающем признаке: коллапс,
нарушение сознания, тяжёлая одышка, неконтролируемое кровотечение, судорожный статус,
отсутствие мочеиспускания у кота с безрезультатными попытками, быстро нарастающее
вздутие с непродуктивными рвотными позывами или другой очевидный красный флаг.
Порода, возраст, один фактор риска, свежая кровь в кале или лёгкое вздутие сами по себе
не означают EMERGENCY.

Верни только JSON:
{"stage":"INTERVIEW|ASSESSMENT|EMERGENCY","questions":["..."],"note":"..."}
"""


def _load_bot_module():
    path = Path(__file__).with_name("main.ру")
    loader = SourceFileLoader("mydoctor_main", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def _extract_text(response_input) -> str:
    chunks: list[str] = []
    if not isinstance(response_input, list):
        return ""
    for message in response_input:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}:
                    text = part.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
    return "\n".join(chunks)


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
            return content
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "input_text":
                    value = part.get("text")
                    if isinstance(value, str):
                        texts.append(value)
            if texts:
                return "\n".join(texts)
    return ""


def _has_media(response_input) -> bool:
    if not isinstance(response_input, list):
        return False
    for message in response_input:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"input_file", "input_image"}:
                    return True
    return False


def _budget_requested(text: str) -> bool:
    value = (text or "").lower().replace("ё", "е")
    markers = (
        "бюджет", "денег", "дешев", "дешевле", "стоимость", "цена", "дорого",
        "только один анализ", "только одно исследование", "могу сдать только",
        "могу сделать только", "хватает только", "что сдавать первым из-за",
    )
    return any(marker in value for marker in markers)


def _active_pet_name(instructions: str) -> str:
    match = re.search(r"Данные активного питомца:\s*\n?Имя:\s*([^;\n]+)", instructions or "")
    return match.group(1).strip() if match else ""


def _explicit_patient_reference(text: str, instructions: str) -> bool:
    value = (text or "").lower().replace("ё", "е")
    pet_name = _active_pet_name(instructions).lower().replace("ё", "е")
    if pet_name and pet_name in value:
        return True
    markers = (
        "у моего", "у моей", "у нашего", "у нашей", "мой пес", "моя собака",
        "мой кот", "моя кошка", "мой питомец", "моя питом", "ему ", "ей ",
        "у него", "у нее", "для него", "для нее", "в его случае", "в ее случае",
        "в моей ситуации", "в нашем случае", "по его анализ", "по ее анализ",
    )
    return any(marker in value for marker in markers)


def _strip_active_pet_context(instructions: str) -> str:
    return re.sub(
        r"\n\nДанные активного питомца:\n.*$",
        "",
        instructions or "",
        flags=re.DOTALL,
    )


def _parse_controller_output(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"stage": "INTERVIEW", "questions": [], "note": "Продолжи сбор анамнеза."}

    if not isinstance(data, dict):
        return {"stage": "INTERVIEW", "questions": [], "note": "Продолжи сбор анамнеза."}

    stage = str(data.get("stage", "INTERVIEW")).upper()
    if stage not in {"INTERVIEW", "ASSESSMENT", "EMERGENCY"}:
        stage = "INTERVIEW"
    questions = data.get("questions") if isinstance(data.get("questions"), list) else []
    return {
        "stage": stage,
        "questions": [str(q).strip() for q in questions if str(q).strip()][:4],
        "note": str(data.get("note", "")).strip(),
    }


def _phase_instruction(decision: dict, budget_requested: bool = False) -> str:
    stage = decision["stage"]
    questions = decision.get("questions", [])
    note = decision.get("note", "")

    if stage == "INTERVIEW":
        if questions:
            numbered = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
            return (
                "\n\nТЕКУЩАЯ СТАДИЯ: INTERVIEW. Сейчас запрещено давать заключение, диагностику, "
                "лечение и памятку. Ответь одной короткой фразой и задай только эти вопросы:\n"
                + numbered + (f"\nВнутренняя подсказка: {note}" if note else "")
            )
        return "\n\nТЕКУЩАЯ СТАДИЯ: INTERVIEW. Задай 2–4 наиболее полезных уточняющих вопроса."

    if stage == "EMERGENCY":
        return (
            "\n\nТЕКУЩАЯ СТАДИЯ: EMERGENCY. Укажи конкретный красный флаг и дай краткую "
            "срочную рекомендацию. " + (f"Внутренняя подсказка: {note}" if note else "")
        )

    budget_note = (
        " Если пользователь сам указал ограничение бюджета, расставь исследования по приоритету."
        if budget_requested else
        " Не упоминай бюджет, цену или экономию, потому что пользователь об этом не спрашивал."
    )
    return (
        "\n\nТЕКУЩАЯ СТАДИЯ: ASSESSMENT. Дай структурированный план: предварительная оценка; "
        "вероятные причины; что можно сделать сейчас; что проверить и зачем; контроль."
        + budget_note
        + (f" Внутренняя подсказка: {note}" if note else "")
    )


class _ResponsesWithKnowledge:
    def __init__(self, responses):
        self._responses = responses

    def _controller_decision(self, kwargs, protocol: str) -> dict:
        instructions = CLINICAL_CONTROLLER + protocol + PATIENT_FACT_RULES
        base = kwargs.get("instructions") or ""
        if base:
            instructions += "\n\nДанные пациента/системные правила:\n" + base
        try:
            response = self._responses.create(
                model=kwargs.get("model", "gpt-5.6-sol"),
                instructions=instructions,
                input=controller_input(kwargs.get("input")),
            )
            return _parse_controller_output(response.output_text)
        except Exception:
            return {"stage": "INTERVIEW", "questions": [], "note": "Продолжи сбор анамнеза."}

    def create(self, *args, **kwargs):
        text = _extract_text(kwargs.get("input"))
        latest_text = _latest_user_text(kwargs.get("input"))
        # Owner statements, not earlier model hypotheses, determine retrieval.
        owner_text = _extract_text([m for m in (kwargs.get("input") or [])
                                   if isinstance(m, dict) and m.get("role") == "user"])
        protocol = protocol_context(latest_text) or protocol_context(owner_text)
        diagnostics = diagnostics_context()
        messages = kwargs.get("input") or []
        recent_marker = (len(messages) >= 2 and isinstance(messages[-2], dict)
                         and isinstance(messages[-2].get("content"), str)
                         and messages[-2]["content"].startswith("[DIAGNOSTICS_MODE]"))
        direct_diagnostics = is_diagnostics_query(latest_text) or recent_marker
        instructions = kwargs.get("instructions") or ""
        budget_requested = _budget_requested(latest_text)

        if direct_diagnostics:
            if not _explicit_patient_reference(latest_text, instructions):
                instructions = _strip_active_pet_context(instructions)
            instructions += DIAGNOSTICS_MODE + diagnostics + protocol
            if budget_requested:
                instructions += (
                    "\nПользователь сам обозначил ограничение бюджета/стоимости. "
                    "В этом ответе можно расставить исследования по приоритету."
                )
            else:
                instructions += (
                    "\nПользователь не спрашивал про бюджет. Не добавляй фразы «если бюджет ограничен», "
                    "«сначала из-за стоимости» и другие финансовые оговорки."
                )
            kwargs["instructions"] = instructions
            return self._responses.create(*args, **kwargs)

        instructions += CLINICAL_REASONING_MODE
        if protocol:
            instructions += protocol

        if not _has_media(kwargs.get("input")) and text.strip():
            decision = self._controller_decision(kwargs, protocol)
            if decision["stage"] == "ASSESSMENT":
                instructions += diagnostics
            instructions += _phase_instruction(decision, budget_requested)

        kwargs["instructions"] = instructions
        return self._responses.create(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._responses, name)


class _ClientWithKnowledge:
    def __init__(self, client):
        self._client = client
        self.responses = _ResponsesWithKnowledge(client.responses)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _install_diagnostics_ui(bot):
    bot.MENU = bot.ReplyKeyboardMarkup(
        [
            ["🩺 Описать симптомы", "📄 Загрузить анализы"],
            ["🧪 Анализы и обследования", "💊 Разобрать назначения"],
            ["🐾 Мои питомцы", "➕ Добавить питомца"],
            ["👨‍⚕️ Записаться на консультацию"],
        ],
        resize_keyboard=True,
    )

    original_message = bot.message
    original_ask_ai = bot.ask_ai

    async def wrapped_ask_ai(update, context):
        if context.user_data.pop("diagnostics_mode", False):
            history = context.user_data.setdefault("history", [])
            history.append({
                "role": "user",
                "content": "[DIAGNOSTICS_MODE] Пользователь выбирает или сравнивает анализы/обследования."
            })
        return await original_ask_ai(update, context)

    async def wrapped_message(update, context):
        text = update.message.text if update.message else ""
        if text == "🧪 Анализы и обследования":
            await bot.ensure_current_user(update)
            context.user_data["diagnostics_mode"] = True
            await update.message.reply_text(
                "Напишите, что хотите сравнить или что нужно выбрать.\n\n"
                "Например:\n"
                "• ПЦР или ИФА — в чём разница?\n"
                "• ОАМ или посев мочи?\n"
                "• УЗИ или рентген?\n"
                "• ОАК или биохимия — что показывает каждый анализ?",
                reply_markup=bot.MENU,
            )
            return
        return await original_message(update, context)

    bot.ask_ai = wrapped_ask_ai
    bot.message = wrapped_message


def main():
    bot = _load_bot_module()
    _install_diagnostics_ui(bot)
    if bot.client is not None:
        bot.client = _ClientWithKnowledge(bot.client)
    bot.main()


if __name__ == "__main__":
    main()
