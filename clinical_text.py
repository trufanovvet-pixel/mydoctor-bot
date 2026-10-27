"""Conservative symptom negation for retrieval, never for rewriting owner facts."""
import re


def normalize(text):
    return str(text or '').lower().replace('ё', 'е')


# Absence of urine, appetite, breathing or consciousness is itself a red flag.
# Those concepts must NOT be removed by a generic negation filter.
_SYMPTOM = (r'(?:рвот\w*|понос\w*|диаре\w*|кашл\w*|зуд\w*|судорог\w*|'
            r'хромот\w*|кровотеч\w*|одышк\w*|боли|боль|болей|выделени\w*|'
            r'слабост\w*|вялост\w*|vomiting|diarrh[oe]+a|cough(?:ing)?|'
            r'itching|seizures?|bleeding|pain)')
_MODIFIER = r'(?:(?:сильн\w*|выраженн\w*|повторн\w*|обильн\w*|severe|any)\s+)?'
_LIST = _MODIFIER + _SYMPTOM + r'(?:\s*(?:,|и|или|ни|or|and)\s*' + _MODIFIER + _SYMPTOM + r')*'
_ABSENT = r'(?:нет|без|отсутству\w*|не было|не наблюда\w*|не отмеча\w*|отрица\w*|no|without)'


def retrieval_text(text):
    """Suppress explicit denials; retain symptoms formulated as inability/loss."""
    value = normalize(text)
    value = re.sub(r'\b' + _LIST + r'\s+(?:нет|не было|отсутству\w*|не наблюда\w*)\b', ' ', value)
    value = re.sub(r'\b' + _ABSENT + r'\s+' + _LIST + r'\b', ' ', value)
    value = re.sub(r'\bне\s+(?:(?:сейчас|уже|больше)\s+)?(?:рвет|рвало|кашля\w*|чешется|хромает|щур\w*|задыха\w*)\b', ' ', value)
    value = re.sub(r'\b(?:not|isn\W?t|doesn\W?t)\s+(?:vomit\w*|cough\w*|itch\w*)\b', ' ', value)
    return value


def clauses(text):
    return re.split(r'[.!?;\n]|\b(?:но|однако|зато|but|however)\b', text)
