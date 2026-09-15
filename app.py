import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

from knowledge import protocol_context


CLINICAL_REASONING_MODE = """

РЕЖИМ КЛИНИЧЕСКОГО ПРИЁМА
Веди диалог как обычный ветеринарный врач на первичном приёме.
Не пытайся за одно сообщение одновременно собрать анамнез, поставить диагноз,
назначить обследование и дать памятку.

Если текущая стадия INTERVIEW:
- дай максимум одну короткую вводную фразу;
- затем задай 2–4 конкретных вопроса;
- не пиши диагнозы, дифференциалы, анализы, УЗИ/рентген, лечение, домашние советы,
  прогноз, «срочно/несрочно» и список красных флагов;
- не повторяй уже известные факты и не задавай вопрос, на который ответ уже есть.

Если текущая стадия ASSESSMENT:
- кратко сформулируй синдром/локализацию, если она обоснована;
- назови только 2–4 наиболее вероятные причины;
- предложи следующий рациональный диагностический шаг и объясни, зачем он нужен;
- не превращай ответ в общий справочник.

Если текущая стадия EMERGENCY:
- кратко укажи конкретный уже описанный признак, делающий ситуацию экстренной;
- рекомендуй срочную очную помощь;
- не перечисляй редкие диагнозы без оснований.

Пиши живым профессиональным русским языком, как врач разговаривает с владельцем.
"""


CLINICAL_CONTROLLER = """
Ты — скрытый клинический диспетчер ветеринарного приёма. Твой ответ не увидит владелец.
Проанализируй ВСЮ доступную переписку по текущей жалобе и выбери только одну стадию:
INTERVIEW, ASSESSMENT или EMERGENCY.

Главное правило: не спеши переходить к ASSESSMENT. Обычный первичный приём должен
сначала собрать достаточный анамнез. Если остаются клинически важные пробелы,
выбирай INTERVIEW и сформулируй 2–4 следующих вопроса, которые сильнее всего изменят
дифференциальный ряд или дальнейшую тактику.

Для INTERVIEW оцени, собраны ли:
- характеристика ведущего симптома;
- начало, длительность, частота и динамика;
- важные примеси/особенности симптома;
- связанные симптомы и общее состояние;
- аппетит/вода по ситуации;
- возможная диетическая погрешность, инородное тело, токсин или новое лекарство,
  если это релевантно;
- хронические болезни/принимаемые препараты, если они могут менять оценку.
Не спрашивай всё подряд: выбери только то, чего реально не хватает сейчас.
Нормально провести 2–3 круга вопросов.

ASSESSMENT выбирай только если данных уже достаточно, чтобы осмысленно назвать
синдром/локализацию и несколько вероятных причин без гадания.

EMERGENCY выбирай только при уже описанном явном красном флаге: коллапс,
нарушение сознания, тяжёлая одышка, неконтролируемое кровотечение, судорожный статус,
отсутствие мочеиспускания у кота с безрезультатными попытками, быстро нарастающее
вздутие живота с повторными непродуктивными рвотными позывами или другой очевидный
жизнеугрожающий признак из предоставленного протокола. Порода, возраст, один фактор
риска, свежая кровь в кале, частая рвота/диарея или лёгкое вздутие сами по себе
не означают EMERGENCY.

Верни ТОЛЬКО JSON без Markdown:
{
  "stage": "INTERVIEW|ASSESSMENT|EMERGENCY",
  "questions": ["вопрос 1", "вопрос 2"],
  "note": "краткое указание для модели, что делать дальше"
}
Для ASSESSMENT и EMERGENCY questions может быть пустым массивом.
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
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"input_text", "output_text"}:
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
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"input_file", "input_image"}:
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
        return {
            "stage": "INTERVIEW",
            "questions": [],
            "note": "Продолжи сбор анамнеза короткими клинически полезными вопросами.",
        }

    stage = str(data.get("stage", "INTERVIEW")).upper()
    if stage not in {"INTERVIEW", "ASSESSMENT", "EMERGENCY"}:
        stage = "INTERVIEW"

    questions = data.get("questions")
    if not isinstance(questions, list):
        questions = []
    questions = [str(q).strip() for q in questions if str(q).strip()][:4]

    return {
        "stage": stage,
        "questions": questions,
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
                "\n\nТЕКУЩАЯ СТАДИЯ: INTERVIEW.\n"
                "Сейчас запрещено давать клиническое заключение, список диагнозов, "
                "обследования, лечение или памятку. Ответь одной короткой естественной "
                "фразой и задай ТОЛЬКО эти вопросы (можно слегка переформулировать для живой речи):\n"
                + numbered
                + (f"\nВнутренняя подсказка: {note}" if note else "")
            )
        return (
            "\n\nТЕКУЩАЯ СТАДИЯ: INTERVIEW.\n"
            "Данных пока недостаточно. Не делай заключений. Задай 2–4 наиболее полезных "
            "уточняющих вопроса по текущей жалобе и уже известным ответам."
        )

    if stage == "EMERGENCY":
        return (
            "\n\nТЕКУЩАЯ СТАДИЯ: EMERGENCY.\n"
            "Начни с краткой срочной рекомендации и укажи конкретный уже описанный "
            "красный флаг, который делает ситуацию экстренной. "
            + (f"Внутренняя подсказка: {note}" if note else "")
        )

    return (
        "\n\nТЕКУЩАЯ СТАДИЯ: ASSESSMENT.\n"
        "Анамнез уже достаточен для первичной клинической оценки. Сформулируй её кратко, "
        "затем наиболее вероятные причины и следующий рациональный шаг. "
        + (f"Внутренняя подсказка: {note}" if note else "")
    )


class _ResponsesWithKnowledge:
    def __init__(self, responses):
        self._responses = responses

    def _controller_decision(self, kwargs, protocol: str) -> dict:
        base_instructions = kwargs.get("instructions") or ""
        controller_instructions = CLINICAL_CONTROLLER
        if protocol:
            controller_instructions += protocol
        if base_instructions:
            controller_instructions += (
                "\n\nДополнительные данные пациента/системные правила из основного запроса:\n"
                + base_instructions
            )

        try:
            response = self._responses.create(
                model=kwargs.get("model", "gpt-5.6-sol"),
                instructions=controller_instructions,
                input=kwargs.get("input"),
            )
            return _parse_controller_output(response.output_text)
        except Exception:
            return {
                "stage": "INTERVIEW",
                "questions": [],
                "note": "Если остаются важные пробелы, продолжи сбор анамнеза.",
            }

    def create(self, *args, **kwargs):
        text = _extract_text(kwargs.get("input"))
        protocol = protocol_context(text)
        instructions = (kwargs.get("instructions") or "") + CLINICAL_REASONING_MODE

        if protocol:
            instructions += protocol

        # Разбор фото/PDF — отдельная задача; клинический интервью-контроллер там не нужен.
        if not _has_media(kwargs.get("input")) and text.strip():
            decision = self._controller_decision(kwargs, protocol)
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


def main():
    bot = _load_bot_module()
    if bot.client is not None:
        bot.client = _ClientWithKnowledge(bot.client)
    bot.main()


if __name__ == "__main__":
    main()
