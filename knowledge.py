import json
from pathlib import Path


BASE_DIR = Path(__file__).parent
PROTOCOL_DIR = BASE_DIR / "protocols"
DIAGNOSTICS_FILE = BASE_DIR / "diagnostics" / "test_comparisons.json"


def _load_protocols() -> list[dict]:
    protocols: list[dict] = []
    if not PROTOCOL_DIR.exists():
        return protocols
    for path in sorted(PROTOCOL_DIR.glob("*.json")):
        try:
            protocols.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return protocols


def _load_diagnostics() -> dict:
    try:
        return json.loads(DIAGNOSTICS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


PROTOCOLS = _load_protocols()
DIAGNOSTICS = _load_diagnostics()


DIAGNOSTIC_TERMS = (
    "[diagnostics_mode]", "анализ", "анализы", "обследован", "пцр", "ифа",
    "серолог", "оак", "общий анализ крови", "биохим", "моч", "посев",
    "upc", "узи", "ультразвук", "рентген", "кал", "фекал", "что сдавать",
    "что лучше", "какой тест", "какой анализ", "бюджет", "дешевле", "дорого",
)


def is_diagnostics_query(text: str) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return any(term in normalized for term in DIAGNOSTIC_TERMS)


def diagnostics_context() -> str:
    if not DIAGNOSTICS:
        return ""
    return (
        "\n\nПРОВЕРЕННАЯ БАЗА СРАВНЕНИЯ АНАЛИЗОВ И ОБСЛЕДОВАНИЙ:\n"
        + json.dumps(DIAGNOSTICS, ensure_ascii=False)
        + "\n\nИспользуй эту базу как правила выбора, а не как текст для дословного пересказа. "
          "Объясняй владельцу простыми словами: что показывает каждый тест, когда он полезнее, "
          "какие есть ограничения и какой тест сдавать первым. Если бюджет ограничен, расставляй "
          "исследования по приоритету исходя из того, что сильнее всего изменит ближайшую тактику. "
          "Не называй один метод универсально лучшим. Если для выбора ПЦР/ИФА или другого теста "
          "нужно знать конкретную инфекцию, срок болезни, вакцинацию или материал — сначала уточни это."
    )


def match_protocols(text: str, limit: int = 2) -> list[dict]:
    normalized = (text or "").lower().replace("ё", "е")
    scored: list[tuple[int, dict]] = []

    for protocol in PROTOCOLS:
        score = 0
        for trigger in protocol.get("triggers", []):
            normalized_trigger = str(trigger).lower().replace("ё", "е")
            if normalized_trigger and normalized_trigger in normalized:
                score += 1
        if score:
            scored.append((score, protocol))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [protocol for _, protocol in scored[:limit]]


def protocol_context(text: str) -> str:
    matches = match_protocols(text)
    if not matches:
        return ""

    chunks = []
    for protocol in matches:
        guidance = str(protocol.get("llm_guidance", "")).strip()
        if guidance:
            chunks.append(guidance)

    if not chunks:
        return ""

    return (
        "\n\nПРОВЕРЕННАЯ МЕДИЦИНСКАЯ БАЗА ДЛЯ ЭТОГО ОБРАЩЕНИЯ:\n"
        + "\n\n".join(chunks)
        + "\n\nИспользуй эту базу как клинические правила и ограничения, а не как текст для пересказа. "
          "Не выгружай владельцу весь протокол и не перечисляй все возможные диагнозы. "
          "Сначала выбери наиболее вероятную локализацию/синдром и следующий вопрос или шаг, "
          "который действительно меняет решение. Если данных недостаточно — уточни их. "
          "Фактор риска без совместимых клинических признаков не превращает случай в экстренный. "
          "Не придумывай дозировки, критерии или исследования, которых нет в проверенной базе.\n\n"
          "КОГДА СТАДИЯ ASSESSMENT: не заканчивай ответ одной рекомендацией 'обратитесь к ветеринару'. "
          "Дай владельцу полноценный, но компактный план по структуре:\n"
          "1. Предварительная оценка — синдром/локализация и 1 наиболее вероятный рабочий диагноз, если есть основания.\n"
          "2. Возможные причины — 2–4 наиболее вероятных дифференциальных диагноза.\n"
          "3. Что можно сделать сейчас — безопасные практические меры до очного осмотра.\n"
          "4. Что проверить — только оправданные исследования, с кратким объяснением зачем.\n"
          "5. Подготовка — только если она есть в проверенной базе.\n"
          "6. Контроль — что отслеживать дома и что меняет срочность.\n"
          "Не используй 'покажите врачу' как замену клиническому плану."
    )
