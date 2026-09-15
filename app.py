import json
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
- если бюджет ограничен, расставь обследования по приоритету;
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
4. Если бюджет ограничен — прямо напиши: «Сначала», «Вторым этапом», «Можно отложить».
5. Если без конкретной инфекции, срока болезни, вакцинации или материала нельзя выбрать
   ПЦР/ИФА/серологию — задай только эти уточняющие вопросы.
6. Не советуй сдавать всё подряд «для надёжности».
7. Если два исследования дополняют друг друга, объясни это прямо.
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

    stage = str(data.get("stage", "INTERVIEW")).upper()
    if stage not in {"INTERVIEW", "ASSESSMENT", "EMERGENCY"}:
        stage = "INTERVIEW"
    questions = data.get("questions") if isinstance(data.get("questions"), list) else []
    return {
        "stage": stage,
        "questions": [str(q).strip() for q in questions if str(q).strip()][:4],
        "note": str(data.get("note", "")).strip(),
    }


def _phase_instruction(decision: dict) -> str:
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

    return (
        "\n\nТЕКУЩАЯ СТАДИЯ: ASSESSMENT. Дай структурированный план: предварительная оценка; "
        "вероятные причины; что можно сделать сейчас; что проверить и зачем; при ограниченном "
        "бюджете — что сдавать первым; контроль. "
        + (f"Внутренняя подсказка: {note}" if note else "")
    )


class _ResponsesWithKnowledge:
    def __init__(self, responses):
        self._responses = responses

    def _controller_decision(self, kwargs, protocol: str) -> dict:
        instructions = CLINICAL_CONTROLLER + protocol
        base = kwargs.get("instructions") or ""
        if base:
            instructions += "\n\nДанные пациента/системные правила:\n" + base
        try:
            response = self._responses.create(
                model=kwargs.get("model", "gpt-5.6-sol"),
                instructions=instructions,
                input=kwargs.get("input"),
            )
            return _parse_controller_output(response.output_text)
        except Exception:
            return {"stage": "INTERVIEW", "questions": [], "note": "Продолжи сбор анамнеза."}

    def create(self, *args, **kwargs):
        text = _extract_text(kwargs.get("input"))
        protocol = protocol_context(text)
        diagnostics = diagnostics_context()
        direct_diagnostics = is_diagnostics_query(text)
        instructions = kwargs.get("instructions") or ""

        if direct_diagnostics:
            instructions += DIAGNOSTICS_MODE + diagnostics
            kwargs["instructions"] = instructions
            return self._responses.create(*args, **kwargs)

        instructions += CLINICAL_REASONING_MODE
        if protocol:
            instructions += protocol

        if not _has_media(kwargs.get("input")) and text.strip():
            decision = self._controller_decision(kwargs, protocol)
            if decision["stage"] == "ASSESSMENT":
                instructions += diagnostics
            instructions += _phase_instruction(decision)

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
                "content": "[DIAGNOSTICS_MODE] Пользователь выбирает анализы/обследования. "
                           "Сравни методы и при ограниченном бюджете расставь приоритеты."
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
                "• ПЦР или ИФА — что лучше в моей ситуации?\n"
                "• ОАМ или посев мочи?\n"
                "• УЗИ или рентген?\n"
                "• Бюджет ограничен — что сдавать первым?",
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
