import json
from pathlib import Path


PROTOCOL_DIR = Path(__file__).with_name("protocols")


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


PROTOCOLS = _load_protocols()


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
          "1. Предварительная оценка — синдром/локализация и 1 наиболее вероятный рабочий диагноз, "
          "если для него достаточно оснований; если нет — так и напиши.\n"
          "2. Возможные причины — 2–4 наиболее вероятных дифференциальных диагноза по убыванию вероятности.\n"
          "3. Что можно сделать сейчас — только безопасные практические меры до очного осмотра, "
          "которые действительно следуют из протокола. Не оставляй этот раздел пустым, если есть "
          "разумная поддерживающая рекомендация.\n"
          "4. Что проверить — только исследования, которые оправданы этим случаем. Для каждого кратко "
          "укажи, какую гипотезу оно помогает подтвердить или исключить. Не назначай автоматически "
          "ОАК, биохимию, УЗИ, ПЦР и анализ кала всем подряд.\n"
          "5. Подготовка — если рекомендовано конкретное исследование, кратко укажи подготовку только "
          "если она есть в проверенной базе; если её в базе нет, не придумывай.\n"
          "6. Контроль — что отслеживать дома и какой признак должен изменить тактику/сделать визит срочным.\n"
          "Не используй 'покажите врачу' как замену клиническому плану. Очный осмотр может быть частью "
          "плана, но рядом должны быть понятны цель визита, предполагаемые исследования и что делать до него."
    )
