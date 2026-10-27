"""Free, deterministic safety notices when a paid consultation cannot start.

This is a small red-flag safety net, not a diagnosis or a complete triage system.
No model call, medication dose, payment, or persisted patient inference is needed.
Sources: Cornell Feline Health Center (urethral obstruction); MSD Veterinary
Manual (corneal disease, respiratory emergencies); existing exotic safety rules.
"""
import re

from clinical_text import normalize, retrieval_text, clauses
from exotic_medicine import detect_species, assess_safety


def free_safety_notice(text, language=None):
    original = normalize(text)
    value = retrieval_text(text)
    if language is None:
        language = 'ru' if re.search('[а-я]', original) else 'en'
    english = language == 'en'
    # Educational or resolved-history clauses must not become a current diagnosis.
    active = []
    for part in clauses(value):
        if re.search(r'\b(?:что такое|объясни понятие|для учебы|what is|explain the term)\b', part):
            continue
        if re.search(r'\b(?:раньше|в прошлом|год назад|месяц назад|used to|last year)\b', part) and not re.search(r'\b(?:сейчас|снова|now|again)\b', part):
            continue
        active.append(part)
    current = '. '.join(active)
    notices = []
    urinary = re.search(
        r'\b(?:не может (?:помочиться|пописать)|не (?:мочится|писает)|'
        r'моч\w*\s+(?:совсем\s+)?(?:нет|не (?:выходит|отходит|вижу))|нет мочи|'
        r'(?:cannot|can\W?t|unable to) (?:urinate|pee)|no urine)\b', current)
    if urinary:
        notices.append(('Если питомец сейчас безуспешно пытается помочиться или моча не выходит, '
                        'везите его в ветеринарную клинику немедленно. Это может быть закупорка; '
                        'несколько капель не исключают её. Не ждите утра, не давите на живот '
                        'и не пытайтесь заменить помощь но-шпой или другими лекарствами.') if not english else
                       ('If your pet is trying but unable to pass urine, go to an emergency vet now. '
                        'A blockage is possible even if a few drops pass. Do not wait until morning, '
                        'press on the abdomen, or substitute home medication for veterinary care.'))
    if re.search(r'\b(?:задыха\w*|тяжело дыш\w*|не может дышать|без сознания|'
                 r'потерял\w* сознание|struggling to breathe|cannot breathe|unconscious)\b', current):
        notices.append(('При затруднённом дыхании или потере сознания нужна экстренная ветеринарная помощь сейчас. '
                        'Минимизируйте стресс и фиксацию, не вливайте воду, корм или лекарства в рот.') if not english else
                       ('Difficulty breathing or loss of consciousness requires emergency veterinary care now. '
                        'Minimize stress and restraint; do not force food, water, or medication into the mouth.'))
    wound_context = re.search(r'\b(?:шов|шва|шве|швы|швов|ран\w*|операц\w*|incision|wound|sutur\w*|surgery)\b', current)
    if wound_context:
        bleeding = re.search(r'\b(?:(?:сильн\w*|обильн\w*|непрерывн\w*) кровотеч\w*|'
            r'кровотеч\w* не останавливается|кровь не останавливается|'
            r'(?:heavy|uncontrolled|continuous) bleeding|bleeding (?:will not|won\W?t|does not) stop)\b', current)
        opening = re.search(r'\b(?:(?:шов|швы|края (?:раны|шва))\s+(?:разош\w*|расход\w*)|'
            r'(?:incision|wound|sutures?)\s+(?:(?:has|have)\s+)?(?:opened|split|come apart))\b', current)
        if bleeding:
            notices.append(('При сильном продолжающемся кровотечении из раны нужна экстренная ветеринарная помощь сейчас. '
                            'Свяжитесь с ближайшей клиникой и выезжайте; не ждите разбора фото.') if not english else
                           'Heavy ongoing wound bleeding needs emergency veterinary care now. Contact the nearest clinic and go; do not wait for photo analysis.')
        elif opening:
            notices.append(('Если края послеоперационного шва разошлись, свяжитесь с оперировавшей клиникой сейчас для осмотра сегодня. '
                            'При глубоком раскрытии раны, видимых внутренних органах, сильном кровотечении или коллапсе выезжайте немедленно. '
                            'Не раздвигайте края и не пытайтесь закрыть шов самостоятельно.') if not english else
                           ('An opened surgical incision needs a call to the operating clinic now for assessment today. '
                            'Go immediately for a deeply open wound, exposed organs, heavy bleeding or collapse. Do not pull the edges apart or close it yourself.'))
    eye_pain = re.search(r'\b(?:щур\w*|болит глаз|боль в глазу|не открывает глаз|squint\w*|painful eye)\b', current)
    if eye_pain:
        notices.append(('Болезненный или прищуренный глаз требует осмотра сегодня, особенно при помутнении или травме. '
                        'Не давайте тереть глаз; наденьте защитный воротник, если он есть. '
                        'До исключения язвы роговицы не используйте капли с дексаметазоном, в том числе Тобрадекс.') if not english else
                       ('A painful or squinting eye needs examination today, especially after injury or with cloudiness. '
                        'Prevent rubbing and use a protective collar if available. Do not use dexamethasone drops, '
                        'including Tobradex, until a corneal ulcer has been ruled out.'))
    species = detect_species(current)
    exotic = assess_safety(current, species)
    if not notices and exotic.urgency in ('immediate', 'same_day'):
        if english:
            notices.append('These signs in this species need veterinary assessment ' +
                           ('immediately.' if exotic.urgency == 'immediate' else 'today.') +
                           ' Contact a veterinarian experienced with this species; do not delay for an online reply.')
        else:
            notices.extend(exotic.reasons)
            notices.extend(exotic.restrictions)
    if not notices:
        return ''
    heading = 'Бесплатная памятка по срочности' if not english else 'Free urgent-care guidance'
    ending = ('Это краткая памятка по описанным признакам, а не диагноз. Не откладывайте очную помощь ради оплаты или ответа в чате.'
              if not english else 'This brief guidance is based on the reported signs, not a diagnosis. Do not delay veterinary care for payment or a chat reply.')
    return heading + '\n\n' + '\n\n'.join(notices) + '\n\n' + ending


def current_user_text(update, model_input):
    """Text/caption first, or the latest transcribed user input for voice messages."""
    message = update.effective_message
    text = getattr(message, 'text', None) or getattr(message, 'caption', None)
    if text:
        return str(text)
    if isinstance(model_input, list):
        for item in reversed(model_input):
            if isinstance(item, dict) and item.get('role') == 'user':
                content = item.get('content', '')
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return '\n'.join(str(part.get('text', '')) for part in content
                                     if isinstance(part, dict) and part.get('type') in ('text', 'input_text'))
    return ''
