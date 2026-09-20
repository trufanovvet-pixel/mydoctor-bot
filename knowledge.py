from medical_evidence import safe_protocol, evidence_context
import json
import logging
import re
from functools import lru_cache
from pathlib import Path

import pymorphy3

BASE_DIR = Path(__file__).parent
PROTOCOL_DIR = BASE_DIR / "protocols"
DIAGNOSTICS_FILE = BASE_DIR / "diagnostics" / "test_comparisons.json"
LAB_INTERPRETATION_FILE = BASE_DIR / "diagnostics" / "lab_interpretation.json"


def _load_protocols() -> list[dict]:
    protocols: list[dict] = []
    if not PROTOCOL_DIR.exists():
        return protocols
    for path in sorted(PROTOCOL_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data.get("title") or not isinstance(data.get("triggers"), list):
                raise ValueError("expected a title and a trigger list")
            protocols.append(data)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Invalid protocol: {path.name}") from exc
    logging.getLogger(__name__).info("Loaded %d medical protocols", len(protocols))
    return protocols


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


PROTOCOLS = _load_protocols()
DIAGNOSTICS = _load_json(DIAGNOSTICS_FILE)
LAB_INTERPRETATION = _load_json(LAB_INTERPRETATION_FILE)
ALIASES = _load_json(BASE_DIR / "protocol_aliases.json")
_MORPH = pymorphy3.MorphAnalyzer()

DIAGNOSTIC_TERMS = (
    "[diagnostics_mode]", "анализ", "анализы", "обследован", "пцр", "ифа",
    "серолог", "оак", "общий анализ крови", "биохим", "моч", "посев",
    "upc", "узи", "ультразвук", "рентген", "кал", "фекал", "что сдавать",
    "что лучше", "какой тест", "какой анализ", "бюджет", "дешевле", "дорого",
)


def _normalize(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    return " ".join(re.findall(r"[a-zа-я0-9%+.-]+", text))


def is_diagnostics_query(text: str) -> bool:
    normalized = _normalize(text)
    # Symptoms (моча, кал) and price words alone must not bypass clinical triage.
    return bool(re.search(
        r"\b(?:анализ\w*|обследован\w*|пцр|ифа|серолог\w*|оак|оам|биохим\w*|"
        r"посев\w*|upc|узи|ультразвук\w*|рентген\w*|референс\w*)\b", normalized
    ))


@lru_cache(maxsize=8192)
def _lemma(word: str) -> str:
    if word.startswith("катаракт"):
        return "катаракта"
    return _MORPH.parse(word)[0].normal_form if re.fullmatch(r"[а-я]+", word) else word


@lru_cache(maxsize=4096)
def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_lemma(w) for w in re.findall(r"[a-zа-я0-9]+", _normalize(text)))


def diagnostics_context() -> str:
    chunks = []
    if DIAGNOSTICS:
        chunks.append("БАЗА ПРОЕКТА (ПОЛНАЯ ПРОВЕРКА ИСТОЧНИКОВ НЕ ЗАВЕРШЕНА): СРАВНЕНИЕ АНАЛИЗОВ И ОБСЛЕДОВАНИЙ:\n" + json.dumps(DIAGNOSTICS, ensure_ascii=False))
    if LAB_INTERPRETATION:
        chunks.append("БАЗА ПРОЕКТА (ПОЛНАЯ ПРОВЕРКА ИСТОЧНИКОВ НЕ ЗАВЕРШЕНА): ИНТЕРПРЕТАЦИЯ ГОТОВЫХ АНАЛИЗОВ:\n" + json.dumps(LAB_INTERPRETATION, ensure_ascii=False))
    if not chunks:
        return ""
    return (
        "\n\n" + "\n\n".join(chunks)
        + "\n\nИспользуй базу как вспомогательные клинические сведения, а не подтверждение каждого утверждения. Не перечисляй показатели механически: связывай изменения в паттерны, "
          "учитывай артефакты, клинику и динамику. Не называй один тест универсально лучшим. "
          "Не упоминай бюджет, цену или экономию, если пользователь сам этого не спрашивал."
    )


def _protocol_score(protocol: dict, normalized: str) -> int:
    score = 0
    tokens = _tokens(normalized)
    words = set(tokens)
    # Curated combinations with a verified interaction must survive top-k retrieval.
    for combination in protocol.get('priority_triggers', []):
        required = set(_tokens(combination))
        if len(required) >= 2 and required <= words:
            score += 20
    triggers = protocol.get("triggers", []) + ALIASES.get(protocol.get("title", ""), [])
    seen = set()
    for trigger in triggers:
        t = _tokens(str(trigger))
        if not t or t in seen:
            continue
        seen.add(t)
        if t == tokens:
            score += 12
        elif any(tokens[i:i + len(t)] == t for i in range(len(tokens) - len(t) + 1)):
            score += 6 if len(t) > 1 else 3
        elif len(t) > 1 and set(t) <= words:
            score += 4
    title = _tokens(str(protocol.get("title", "")))
    if title and title == tokens:
        score += 100
    return score


def match_protocols(text: str, limit: int = 4) -> list[dict]:
    normalized = _normalize(text)
    scored = []
    for protocol in PROTOCOLS:
        score = _protocol_score(protocol, normalized)
        if score:
            scored.append((score, protocol))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return []
    best = scored[0][0]
    # Keep relevant comorbid/competing protocols, but suppress weak accidental trigger matches.
    threshold = max(3, best // 3)
    return [p for score, p in scored if score >= threshold][:limit]


def _structured_protocol(protocol: dict) -> str:
    evidence = evidence_context(protocol)
    protocol = safe_protocol(protocol)
    fields = [evidence, "Статус исходного файла: " + str(protocol.get("status", "не указан")),
              "Виды: " + json.dumps(protocol.get("species", []), ensure_ascii=False)]
    title = protocol.get("title")
    if title:
        fields.append(f"ПРОТОКОЛ: {title}")
    mapping = (
        ("goals", "Цели"),
        ("triage_questions", "Ключевые вопросы"),
        ("emergency_red_flags", "Красные флаги"),
        ("differentials", "Дифференциалы"),
        ("diagnostics", "Диагностика"),
        ("management_principles", "Принципы ведения"),
        ("do_not_do", "Не делать"),
    )
    for key, label in mapping:
        value = protocol.get(key)
        if value:
            if isinstance(value, list):
                value = "; ".join(str(x) for x in value)
            fields.append(f"{label}: {value}")
    guidance = str(protocol.get("llm_guidance", "")).strip()
    if guidance:
        fields.append(f"Экспертные ограничения: {guidance}")
    # Specialist catalogs and newer treatment fields have different schemas.
    # Preserve every additional clinical field, including nested data and sources.
    rendered = {key for key, _ in mapping} | {"title", "llm_guidance"}
    metadata = {"id", "status", "version", "reviewed_at", "species", "triggers", "audiences"}
    for key, value in protocol.items():
        if key not in rendered | metadata and value:
            fields.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(fields)


def protocol_context(text: str) -> str:
    matches = match_protocols(text)
    if not matches:
        return ""
    chunks = [_structured_protocol(p) for p in matches]
    return (
        "\n\nМЕДИЦИНСКАЯ БАЗА ПРОЕКТА ДЛЯ ЭТОГО ОБРАЩЕНИЯ:\n"
        + "\n\n".join(chunks)
        + "\n\nРЕЖИМ ЭКСПЕРТНОГО КЛИНИЧЕСКОГО МЫШЛЕНИЯ:\n"
          "Работай не как справочник, а как сильный клиницист. Сначала сформируй problem representation: вид, возраст, длительность, "
          "главные симптомы, тяжесть, динамика и уже известные объективные данные. Затем локализуй проблему по системе/органу и отдели "
          "синдром от диагноза. Построй короткий приоритетный дифференциальный ряд по вероятности и опасности. Не перечисляй редкости без основания.\n"
          "Выбирай следующий вопрос или исследование по его способности реально изменить решение. Не назначай широкую панель 'на всякий случай'. "
          "Учитывай совместимость нескольких заболеваний и не пытайся обязательно объяснить все отклонения одним диагнозом. "
          "Различай корреляцию, фактор риска и причинность. Интерпретируй результаты в контексте pre-test probability и ограничений метода.\n"
          "Следи за динамикой: новый результат, изменение симптомов или ответ на лечение должны обновлять рабочие гипотезы. "
          "Если данные противоречат первоначальной гипотезе, пересмотри ее, а не защищай. Если уверенности недостаточно, прямо обозначь, что известно, "
          "что предполагается и какое следующее действие уменьшит неопределенность.\n"
          "Срочность определяй конкретными красными флагами и физиологической нестабильностью, а не самим названием симптома. Если случай экстренный, "
          "назови конкретный признак и почему промедление опасно. Если пациент стабилен, не запугивай владельца.\n"
          "Не придумывай дозировки, пороги, противопоказания или схемы, которых нет в проверенной базе. При прямом вопросе о дозе и достаточных исходных "
          "данных сначала рассчитай ответ по проверенной информации, затем дай краткие ограничения безопасности.\n"
          "Не подтягивай профиль конкретного питомца в общий вопрос, если пользователь явно не связал вопрос с этим питомцем.\n"
          "Ответ владельцу: сначала прямой ответ. Затем, если нужно: 1) предварительная оценка; 2) 2–4 приоритетные причины; 3) что делать сейчас; "
          "4) что проверить и зачем; 5) контроль и признаки изменения срочности. Задавай только вопросы, ответы на которые меняют тактику. "
          "Не используй 'обратитесь к ветеринару' как замену клиническому плану."
    )
