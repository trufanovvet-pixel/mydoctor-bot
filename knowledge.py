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
        + "\n\nСледуй этой базе приоритетно. Если данных пациента недостаточно, сначала уточни их. "
          "Не придумывай дозировки, критерии или исследования, которых нет в базе."
    )
