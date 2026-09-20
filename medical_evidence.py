"""Field-level evidence receipts. Unreviewed doses remain on disk, not in prompts."""
import hashlib
import json
import re
from pathlib import Path

REGISTRY_PATH = Path(__file__).with_name('medical_evidence.json')
# Concentration of a lab analyte (e.g. cortisol in mcg/dL) is not a drug dose.
DOSE_PATTERN = re.compile(
    r'\d(?:[\d.,–−\- ]*)\s*(?:мг|мкг|мл|mg|mcg|µg|μg|ml|ед|iu|u)\s*'
    r'(?:/\s*(?:кг|kg)\b|на\s+кг\b)', re.I)
BLOCKED = '[Лекарственная схема не подтверждена по полному источнику; не использовать для назначения.]'


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def load_registry():
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'protocols': {}, 'sources': {}}


REGISTRY = load_registry()


def review_for(protocol):
    trigger_hash = digest(json.dumps(protocol.get('triggers', []), ensure_ascii=False))
    return next((r for r in REGISTRY.get('protocols', {}).values()
                 if r.get('title') == protocol.get('title') and r.get('trigger_hash') == trigger_hash), {})


def verified_claim(review, path, value):
    receipt = review.get('claims', {}).get(path, {})
    sources = REGISTRY.get('sources', {})
    return (receipt.get('status') == 'supported' and receipt.get('sha256') == digest(value)
            and bool(receipt.get('source_ids')) and all(
                sources.get(s, {}).get('access') == 'full_text_read'
                for s in receipt.get('source_ids', [])))


def safe_protocol(protocol):
    review = review_for(protocol)

    def walk(value, path=''):
        if isinstance(value, dict):
            return {k: walk(v, path + '/' + k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, path + '/' + str(i)) for i, v in enumerate(value)]
        if isinstance(value, str) and DOSE_PATTERN.search(value):
            if not verified_claim(review, path, value):
                return BLOCKED
        return value

    return walk(protocol)


def evidence_context(protocol):
    review = review_for(protocol)
    lines = ['Статус проверки: ' + review.get('status', 'source_review_pending'),
             'Проверены только явно перечисленные утверждения; весь протокол не сертифицирован.']
    for path, receipt in review.get('claims', {}).items():
        value = protocol
        try:
            for part in path.strip('/').split('/'):
                value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if not isinstance(value, str) or not verified_claim(review, path, value):
            continue
        for sid in receipt['source_ids']:
            source = REGISTRY['sources'][sid]
            lines.append(f"Подтверждение {path}: {source['title']} — {source['url']}; раздел: {receipt['locator']}")
    lines.append('Дозы — материал для ветеринарного контроля, не разрешение назначать их каждому пациенту. '
                 'Без подтверждённого показания, вида, текущего веса, формы, пути, кратности и значимых '
                 'противопоказаний новую схему не выбирай. Не выводи дозу из общей ссылки на учебник. '
                 'Если параметр не указан в источнике, не достраивай его по памяти.')
    return '\n'.join(lines)
