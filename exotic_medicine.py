"""Species scope and conservative triage shared by Telegram, web and documents.

These rules are a safety floor, not a diagnostic classifier. An unmatched phrase
must still be assessed by the clinical model. No drug doses are provided here.
"""
import re
from dataclasses import dataclass

SPECIES_OPTIONS = ('Собака', 'Кошка', 'Птица', 'Кролик', 'Морская свинка', 'Крыса',
                   'Мышь', 'Хомяк', 'Шиншилла', 'Дегу', 'Песчанка', 'Хорёк',
                   'Черепаха', 'Ящерица', 'Змея', 'Другая рептилия', 'Другой грызун', 'Другое')
# Patterns are word bounded. Genitives and plurals are accepted without matching
# unrelated words (e.g. попугаев, but not попугайный as a patient).
ALIASES = {
    'dog': r'собак\w*|п[её]с|пса|псу|щен\w*|dogs?|pupp(?:y|ies)|canine',
    'cat': r'кошк\w*|кот(?:а|у|ом|е|ы|ов)?|кот[её]нок|котят\w*|cats?|kittens?|feline',
    'bird': r'птиц\w*|попуга[йяюеив]\w*|волнист\w*|корелл\w*|жако|канаре[йек]\w*|амадин\w*|неразлучник\w*|какаду|ара|лори|budg\w*|parrots?|cockatiels?|birds?|canar(?:y|ies)|finches?|macaws?',
    'rabbit': r'кролик\w*|крольч\w*|rabbits?|bunn(?:y|ies)',
    'guinea_pig': r'морск\w*\s+свин\w*|guinea\s*pigs?|cavy|cavies',
    'rat': r'крыс(?:а|ы|е|у|ой|ою|ам|ами|ах)?|крысенок|крысят\w*|rats?',
    'mouse': r'мыш(?:ь|и|ей|ами|ам|ах|ка|ки|ке|ку|онок)|mice|mouse',
    'hamster': r'хомяк(?:а|у|ом|е|и|ов|ам|ами|ах)?|хомяч(?:ок|ка|ку|ком|ке|ки|ков|кам|ками|ках)|hamsters?',
    'chinchilla': r'шиншилл\w*|chinchillas?',
    'degu': r'дегу|degus?',
    'gerbil': r'песчанк\w*|gerbils?',
    'rodent': r'грызун\w*|rodents?',
    'ferret': r'хор[её]к|хорьк\w*|фретк\w*|ferrets?',
    'chelonian': r'черепах\w*|красноух\w*|turtles?|tortoises?|chelonians?',
    'lizard': r'ящериц\w*|агам\w*|эублефар\w*|геккон\w*|игуан\w*|хамелеон\w*|сцинк\w*|lizards?|geckos?|iguanas?|bearded\s+dragons?|chameleons?',
    'snake': r'зме[яиюйе]\w*|питон\w*|полоз\w*|удав\w*|snakes?|pythons?|boas?',
    'reptile': r'рептили\w*|reptiles?',
    'unsupported': r'другое|еж(?:ик|ика|у|а)?|ёж\w*|ежих\w*|амфиби\w*|лягуш\w*|hedgehogs?|amphibians?|frogs?'
}
RODENTS = {'guinea_pig','rat','mouse','hamster','chinchilla','degu','gerbil'}
REPTILES = {'chelonian','lizard','snake'}
EXOTICS = {'bird','rabbit','ferret','rodent','reptile'} | RODENTS | REPTILES

def normalize(text):
    return str(text or '').lower().replace('ё','е')

def detect_species(text):
    value = normalize(text)
    # Target animals on a poison label are not patients.
    value = re.sub(r'(?:отрав\w*|яд|приманк\w*)\s+для\s+крыс\w*', '', value)
    found = {key for key, pattern in ALIASES.items() if re.search(r'(?<!\w)(?:'+pattern+r')(?!\w)', value)}
    if found & RODENTS: found.discard('rodent')
    if found & REPTILES: found.discard('reptile')
    return frozenset(found)

def species_hint(instructions):
    """Read only a labelled patient field; never species in system rules."""
    match = re.search(r'(?:^|[;\n])\s*[Вв]ид:\s*([^;\n]+)', instructions or '')
    breed = re.search(r'(?:^|[;\n])\s*[Пп]орода:\s*([^;\n]+)', instructions or '')
    return ' '.join(m.group(1).strip() for m in (match, breed) if m)

def resolve_species(latest, owner_text='', pet_species=''):
    explicit = detect_species(latest)
    if explicit: return explicit
    # A selected, explicitly referenced pet outranks an older case.
    hint = detect_species(pet_species)
    if hint: return hint
    for line in reversed((owner_text or '').splitlines()):
        found = detect_species(line)
        if found: return found
    return frozenset()

def scope_keys(species):
    result = set(species)
    if result & RODENTS: result.add('rodent')
    if result & REPTILES: result.add('reptile')
    return result

def protocol_applies(protocol, species):
    target = set(species)
    declared = set(protocol.get('species', []))
    if protocol.get('id','').startswith('exotic_'):
        # No exotic protocol should displace dog/cat retrieval on a generic symptom.
        return bool(declared & scope_keys(target))
    if target & (EXOTICS | {'unsupported'}):
        return False
    if not target: return True
    known = set().union(*(detect_species(s) for s in declared)) if declared else set()
    return bool(target & known) if known else True

BASE_RULES = '''
ВИДОСПЕЦИФИЧНАЯ БЕЗОПАСНОСТЬ:
До лечебной схемы установи точный вид. Птица, грызун и рептилия — группы, а не точные виды.
Кролик не грызун, хорёк не травоядное. Не переносить дозы, референсы, голодную выдержку,
питание и календарь профилактики собак/кошек на экзотических животных.
Для массы в граммах сначала переведи в кг; мг, мкг и мл не взаимозаменяемы.
В новом модуле нет проверенных дозировок: новую схему по памяти не добавлять.
При противопоказании сначала сообщи ограничение; не вычисляй следующую опасную дозу.
Не требуй завершить опрос до сообщения срочных действий. Отсутствие совпадения
с программным правилом НЕ означает норму: самостоятельно оцени все симптомы.
При неизвестном/смешанном виде уточни пациента, сохрани общую безопасную помощь.
'''

PROFILES = {
'bird': 'Уточнить вид, возраст, вес в граммах/динамику, рацион, помёт, дыхание в покое, кладку и контакт с дымом/аэрозолями. Анализы — по птичьим референсам и подходящему методу, не по собачьим.',
'rabbit': 'Уточнить время последней еды и нормальных фекалий, боль/вздутие, зубы, рацион и лекарства с путём введения. Стаз — синдром; сначала исключить обструкцию. При подозрении не давать прокинетик и не докармливать насильно.',
'guinea_pig': 'Уточнить питание, источник витамина C, динамику веса, зубы, аппетит/фекалии и мочеиспускание. Не сводить анорексию только к дефициту витамина C.',
'rat': 'Уточнить дыхание в покое, вес, подстилку/аммиак и контактных животных. Порфирин не равен крови. Запреты антибиотиков у кролика/морской свинки не переносить автоматически на крысу.',
'mouse': 'Уточнить дыхание, вес, подстилку и группу содержания. Не переносить дозы или антибиотические запреты кроликов автоматически на мышь.',
'hamster': 'Уточнить вид/возраст, диарею, воду, активность, стресс и антибиотики. Мокрый хвост не подтверждает единственную причину.',
'chinchilla': 'Уточнить сено, вес, фекалии, слюнотечение и зубы. Нормальные резцы не исключают заболевания щёчных зубов.',
'degu': 'Уточнить возраст, рацион, зубы, массу и точную жалобу. Покрытие этого вида ограничено общей безопасностью; специальную лекарственную схему запрашивать по отдельному источнику.',
'gerbil': 'Уточнить вид, возраст, рацион, условия группы и жалобу. Покрытие этого вида ограничено общей безопасностью; не подставлять хомячью схему.',
'rodent': 'Обязательно уточнить: морская свинка, крыса, мышь, хомяк, шиншилла, дегу или песчанка. Универсального рациона/антибиотика для грызунов нет.',
'ferret': 'Уточнить питание, кастрацию, вакцинацию, глюкозу при слабости, возможное проглатывание резины и выделение мочи. Не устраивать домашнее голодание при подозрении на гипогликемию.',
'reptile': 'Уточнить точный вид, возраст, вес/динамику, измеренные температуры зон и места прогрева, влажность, UVB (лампа, расстояние, преграды), рацион и сезонность. Не задавать универсальный микроклимат.',
'chelonian': 'Уточнить водная или сухопутная и точный вид, корм, UVB, температуры, плавучесть. Ивермектин противопоказан, мильбемицина избегать.',
'lizard': 'Уточнить точный вид и возраст, рацион/насекомых/добавки, UVB и измеренный микроклимат. При слабости оценивать костную и метаболическую патологию.',
'snake': 'Уточнить вид, возраст, динамику массы, последний корм, линьку, срыгивание и измеренный микроклимат. Стабильная взрослая змея без потери массы не получает срочность только по числу дней без еды.',
'unsupported': 'Вид не поддержан специальными протоколами. Уточнить точное название, безопасно оценить срочность, не использовать схемы другого вида.'
}

def species_context(species):
    if not set(species) & (EXOTICS | {'unsupported'}): return ''
    lines=[BASE_RULES,'Вид/группа по данным владельца: '+', '.join(sorted(species))]
    if len(species)>1: lines.append('Упомянуто несколько видов. Не смешивать пациентов; до лекарства уточнить, кому оно предназначено.')
    for key in sorted(scope_keys(species)):
        if key in PROFILES: lines.append(PROFILES[key])
    return '\n\n'+'\n'.join(lines)

@dataclass(frozen=True)
class SafetyDecision:
    urgency: str = 'unclassified'
    reasons: tuple[str, ...] = ()
    restrictions: tuple[str, ...] = ()

    def prompt(self):
        if not self.reasons and not self.restrictions: return ''
        return ('\n\nОБЯЗАТЕЛЬНАЯ ОЦЕНКА БЕЗОПАСНОСТИ: '+self.urgency+'\n'
                +'\n'.join(self.reasons+self.restrictions)
                +'\nЭто минимальная срочность по сообщённым признакам, не окончательный диагноз. '
                'Сначала объясни действия/ограничения, затем уточняй. Отвечай на языке пользователя.')


def _present(text, pattern):
    """Negation of a positive red flag, without negating 'не ест/не мочится'."""
    for clause in re.split(r'[.!?;\n]|\bно\b|\bbut\b', normalize(text)):
        for m in re.finditer(pattern, clause):
            before=clause[max(0,m.start()-24):m.start()]
            after=clause[m.end():m.end()+18]
            if re.search(r'(?:нет|без|не|no|without|not)\s+(?:\w+\s+)?$',before): continue
            if re.match(r'\s*(?:нет|отсутству\w*|не было|не наблюда\w*|absent)\b',after): continue
            yield m.group()

def assess_safety(text, species):
    if not set(species)&EXOTICS: return SafetyDecision()
    value=normalize(text); keys=scope_keys(species); reasons=[]; restrictions=[]; level='unclassified'
    # General educational questions retain reference mode, never become a current emergency.
    educational=bool(re.search(r'^(?:что такое|расскажи|объясни|what is|tell me about)\b', value.strip())) and not re.search(r'\b(?:сейчас|у моего|у моей|my |right now)',value)
    def flag(urgency,reason):
        nonlocal level
        if educational:return
        priority={'unclassified':0,'same_day':1,'immediate':2}
        if priority[urgency]>priority[level]:level=urgency
        reasons.append(reason)
    def present(pattern): return bool(list(_present(value,pattern)))
    if present(r'судорог\w*|коллапс|без сознания|потерял\w* сознание|seizures?|unconscious|collapse'):
        flag('immediate','Судороги, коллапс или нарушение сознания: немедленная очная помощь; не вливать воду/корм/лекарства в рот.')
    if present(r'задыха\w*|тяжело дыш\w*|дыш\w* (?:с )?открыт\w* (?:клюв\w*|рт\w*)|дыш\w* ртом|дыхани\w* с открыт\w* клюв\w*|open[- ](?:mouth|beak) breathing|struggling to breathe|breathing (?:with )?(?:an? )?open (?:mouth|beak)'):
        flag('immediate','Описано затруднение дыхания: немедленная помощь, минимум фиксации; не докармливать насильно.')
    if present(r'не (?:может )?мочится|не может помочиться|нет мочи|не писает|cannot urinate|no urine'):
        flag('immediate','Отсутствие мочи/безрезультатные попытки мочиться: немедленный осмотр, не ждать до утра.')
    anorexia=present(r'не ест|не кушает|отказ\w* от (?:еды|корма)|перестал\w* есть|not eating|stopped eating')
    if keys&{'rabbit','guinea_pig','chinchilla'} and present(r'не какает|нет кала|нет фекали\w*|no droppings'):
        flag('same_day','Отсутствие фекалий у травоядного требует оценки сегодня, особенно при снижении аппетита.')
    if keys&{'rabbit','guinea_pig','chinchilla','bird'} and anorexia:
        flag('same_day','Отказ от еды у этого вида требует очной оценки сегодня; при слабости, боли, одышке или ухудшении — немедленно.')
    if keys&{'rabbit','guinea_pig','chinchilla'} and (anorexia or present(r'вздут\w*|раздул\w*|bloated')):
        restrictions.append('При выраженном вздутии или подозрении на обструкцию не докармливать насильно и не давать прокинетик до исключения непроходимости.')
        if present(r'вздут\w*|раздул\w*|bloated') and (anorexia or present(r'сильн\w* бол\w*|кричит|коллапс')):
            flag('immediate','Отказ от еды с вздутием: возможна непроходимость, нужна немедленная оценка.')
    if 'bird' in keys and present(r'не держится на жердоч\w*|не может сидеть на жердоч\w*|cannot perch'):
        flag('immediate','Птица не удерживается на жердочке: выраженная слабость, нужна немедленная помощь.')
    if 'bird' in keys and present(r'перегрел\w* (?:антипригарн\w* |тефлонов\w* )?сковород\w*|дым от сковород\w*|overheated nonstick|ptfe fumes'):
        flag('immediate','Возможное вдыхание PTFE: убрать птицу от источника на чистый воздух без переохлаждения и немедленно связаться с клиникой, не ждать симптомов.')
    if 'bird' in keys and present(r'не может снести|застрял\w* яйц\w*|egg[- ]bound|egg binding'):
        flag('immediate','Подозрение на задержку яйца: срочный осмотр; не давить на живот и не извлекать яйцо дома.')
    if 'hamster' in keys and present(r'понос|диаре\w*|мокр\w* хвост|diarrhea|wet tail'):
        flag('same_day','Диарея у хомяка требует осмотра сегодня из-за риска быстрого обезвоживания.')
        if present(r'вял\w*|лежит|слаб\w*|letharg\w*|weak'):
            flag('immediate','Диарея вместе с вялостью у хомяка: немедленная очная помощь.')
    if 'ferret' in keys and present(r'шатает\w*|судорог\w*|коллапс|staggering|seizures?|collapse'):
        flag('immediate','У хорька с такими признаками срочно проверить глюкозу; не проводить домашнюю голодную пробу.')
    if 'rabbit' in keys:
        if re.search(r'фипронил|фронтлайн|fipronil|frontline',value): restrictions.append('Фипронил противопоказан кроликам. Не применять и не рассчитывать дозу; если уже применён — немедленно связаться с врачом.')
        if re.search(r'амоксициллин|амоксиклав|синулокс|ампицилин|ампициллин|клиндамицин|линкомицин|эритромицин|цефалоспорин|amoxicillin|amoxiclav|synulox|clindamycin|lincomycin|ampicillin|erythromycin|cephalosporin',value):
            restrictions.append('У кролика эти антибиотики противопоказаны при пероральном применении. Уточнить действующее вещество и путь; не вычислять следующую дозу внутрь. Не распространять запрет автоматически на все парентеральные пенициллины.')
    if 'guinea_pig' in keys and re.search(r'амоксициллин|амоксиклав|синулокс|пенициллин|клиндамицин|линкомицин|amoxicillin|amoxiclav|synulox|penicillin|clindamycin|lincomycin',value):
        restrictions.append('У морской свинки высокий риск антибиотик-ассоциированной энтеротоксемии: не рекомендовать этот препарат самостоятельно и не рассчитывать следующую дозу. Смена пути не гарантирует безопасность.')
    if 'hamster' in keys and re.search(r'пенициллин|линкомицин|бацитрацин|penicillin|lincomycin|bacitracin',value):
        restrictions.append('У хомяка эти антибиотики могут вызвать энтеротоксемию; не рекомендовать самостоятельное применение.')
    if 'chelonian' in keys and re.search(r'ивермектин|ивермек|ivermectin|ivermek|мильбемицин|milbemycin',value):
        restrictions.append('Ивермектин противопоказан черепахам; мильбемицина также избегать. Дозу не рассчитывать. При уже состоявшемся применении немедленно связаться с врачом по рептилиям.')
    return SafetyDecision(level,tuple(dict.fromkeys(reasons)),tuple(dict.fromkeys(restrictions)))


MEDIA_SPECIES_RULES = """
Для документа сначала установи вид пациента по оригиналу и явно выбранной карточке;
при конфликте уточни принадлежность. Для птиц, кроликов, грызунов, хорьков и рептилий
не использовать референсы, дозы и профилактику собак/кошек. При неизвестном виде
не выбирать лекарственную схему. Отделять видимое на изображении от предположения.
"""
