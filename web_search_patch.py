import os
import re

from openai import OpenAI


LOCAL_SEARCH_RULES = """

РЕЖИМ АКТУАЛЬНОГО ПОИСКА И СРАВНЕНИЯ ВЕТЕРИНАРНЫХ КЛИНИК
Пользователь просит найти, сравнить или выбрать ветеринарную клинику, врача или место,
куда лучше обратиться. Это навигационный/медицинский выбор, а не новый этап клинического опроса.

Обязательно используй веб-поиск и актуальные публичные источники.
Для России приоритет локальных источников: официальный сайт клиники, Яндекс Карты/Яндекс Поиск
и 2ГИС. Для навигации в России давай Яндекс Карты/Навигатор, а при наличии — также 2ГИС.
Google Maps не предлагай, если пользователь сам его не попросил.

КЛЮЧЕВОЕ ПРАВИЛО СРАВНЕНИЯ
Не выбирай клинику только по рейтингу и отзывам. Рейтинг — лишь один из факторов.
Сначала определи, какая помощь нужна пациенту по текущей переписке, а затем оцени,
насколько каждая клиника способна оказать именно эту помощь.

Если текущая переписка относится к конкретному питомцу, учитывай:
- состояние питомца сейчас и наличие красных флагов;
- предполагаемые дифференциальные диагнозы;
- какие исследования, специалисты и процедуры могут понадобиться в ближайшее время;
- нужна ли срочная помощь, стационар, операция, визуальная диагностика или узкий специалист.

При сравнении клиник оцени по совокупности факторов:
1. Клиническое соответствие текущей задаче — самое важное.
   Есть ли именно нужные услуги и возможности для этого случая.
2. Оснащение и диагностическая база.
   Проверяй наличие УЗИ, цифрового рентгена, лаборатории, эндоскопии, КТ/МРТ,
   операционных, ИВЛ, мониторинга, кислорода, стационара и других средств только когда
   они действительно релевантны случаю. Не считай наличие любого дорогого оборудования
   автоматическим признаком лучшей клиники.
3. Узкие специалисты и команда.
   Хирург, анестезиолог, врач визуальной диагностики, кардиолог, невролог, офтальмолог,
   ортопед, реаниматолог и другие специалисты — в зависимости от текущей проблемы.
   Проверяй команду по официальному сайту/странице клиники, если возможно.
4. Возможность оказать срочную помощь.
   Часы работы, работа 24/7, стационар, возможность принять без длительного ожидания,
   наличие экстренной хирургии/реанимации — особенно важно при остром состоянии.
5. Масштаб и инфраструктура клиники.
   Если площадь клиники официально указана — можешь учитывать её как косвенный показатель.
   Если площадь не опубликована, не придумывай её. Вместо этого оцени количество отделений,
   кабинетов, операционных, стационар, спектр диагностики и размер команды.
6. Время работы на рынке и устойчивость.
   Учитывай подтверждённый год основания/срок работы, но не делай его решающим сам по себе.
7. Прозрачность официального сайта.
   Плюсом считай, когда сайт ясно показывает врачей, квалификацию, оборудование, услуги,
   режим работы, контакты и адреса. Красивый дизайн сайта сам по себе не означает качество.
8. Отзывы и рейтинг.
   Учитывай рейтинг, число отзывов, повторяющиеся положительные и отрицательные темы.
   Большое число отзывов обычно информативнее нескольких оценок. Не делай вывод только
   по средней звезде и не выдавай отзывы за доказательство медицинского качества.

При запросе «какая клиника лучше»:
- сравни 2–5 наиболее релевантных клиник по указанным выше критериям;
- сначала сформулируй, что именно важно для текущего пациента;
- затем кратко сравни сильные/слабые стороны каждой клиники;
- в конце назови, какая клиника лучше подходит ИМЕННО ДЛЯ ЭТОЙ СИТУАЦИИ и почему;
- если для другой задачи выбор был бы другим, прямо скажи это;
- если данных недостаточно, не выдумывай — укажи, что конкретный параметр не удалось подтвердить.

Не используй фиксированный математический балл, если данные по клиникам неполные.
Если данных достаточно, можешь использовать внутреннее приоритетное взвешивание:
клиническое соответствие и оснащение важнее специалистов, режим/экстренность важнее
репутационных признаков, а отзывы и рейтинг — вспомогательный фактор.

Если в текущем сообщении или недавней переписке НЕ указаны город, район, адрес,
ориентир или координаты, а пользователь пишет «рядом со мной», «ближайшую»,
«у меня в городе» и т.п. — не угадывай местоположение и не отказывайся от поиска.
Коротко спроси: «Напишите город/район или ближайший ориентир — найду варианты рядом».

Когда местоположение известно:
- найди обычно 3–5 реально существующих ветеринарных клиник поблизости;
- для каждой укажи название, адрес, рейтинг и число отзывов ТОЛЬКО если это удалось
  подтвердить актуальным источником, часы работы, телефон и официальный сайт, если они есть;
- если официальный сайт не найден, так и напиши, не подменяй его случайным каталогом;
- для навигации по России в первую очередь дай ссылку на Яндекс Карты/Навигатор;
- если удалось подтвердить карточку в 2ГИС, дополнительно дай ссылку 2ГИС;
- если пользователь просит круглосуточную/экстренную помощь, отдавай приоритет клиникам,
  для которых удалось подтвердить работу 24/7 или текущие часы;
- не называй точное расстояние или время в пути, если источник этого не подтверждает;
- не придумывай оборудование, специалистов, площадь, год основания, рейтинг, отзывы,
  часы, телефон, адрес или сайт;
- если данные в источниках расходятся, кратко отметь это;
- в срочной ситуации посоветуй перед выездом позвонить и подтвердить, что клиника принимает.

Формат ответа — компактный русский текст, удобный для Telegram. Без Markdown-звёздочек.
Для каждой клиники отдельный короткий блок. URL пиши полностью, чтобы Telegram сделал их кликабельными.
"""


LOCAL_TERMS = (
    "ветклиник", "вет клиник", "ветеринарная клиник", "ветеринарную клиник",
    "ветеринарной клиник", "ветцентр", "вет центр", "ветеринарный центр",
    "ветеринарную помощь", "ветврач", "вет врач", "ветеринар рядом",
    "круглосуточная клиник", "клиника лучше", "какая клиника", "сравни клиник",
)

SEARCH_TERMS = (
    "найди", "найдите", "найти", "ближайш", "рядом", "поблизости", "недалеко",
    "куда обратиться", "куда ехать", "рейтинг", "сайт клиники", "адрес клиники",
    "открыта сейчас", "работает сейчас", "круглосуточ", "какая лучше", "что лучше",
    "лучше клиник", "сравни", "сравнить", "выбрать клиник", "куда лучше",
)


def _plain_text(response_input) -> str:
    if not isinstance(response_input, list):
        return str(response_input or "")
    chunks = []
    for message in response_input:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    value = part.get("text")
                    if isinstance(value, str):
                        chunks.append(value)
    return "\n".join(chunks)


def _needs_local_search(response_input) -> bool:
    messages = response_input if isinstance(response_input, list) else []
    latest = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("role") == "user"), None)
    value = _plain_text([latest] if latest else response_input).lower().replace("ё", "е")
    if not value:
        return False
    if not any(term in value for term in LOCAL_TERMS) and messages:
        previous = [m for m in messages[:-1] if isinstance(m, dict) and m.get("role") == "user"]
        previous_text = _plain_text(previous[-1:]).lower().replace("ё", "е")
        was_search = any(term in previous_text for term in LOCAL_TERMS) and any(term in previous_text for term in SEARCH_TERMS)
        location_followup = bool(re.match(r"^(?:я (?:в|на)|город\b|а в\b|только круглосуточ|покажи на карте)", value))
        if was_search and location_followup:
            return True
    return (
        any(term in value for term in LOCAL_TERMS)
        and any(term in value for term in SEARCH_TERMS)
    )


def _dialog_scope(response_input):
    if not isinstance(response_input, list):
        return None
    scope = None
    for item in response_input:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if content == "[GENERAL_SCOPE]":
            scope = "general"
        elif content == "[PET_SCOPE]":
            scope = "pet"
    return scope


def _active_pet_context(instructions: str) -> str:
    match = re.search(
        r"Данные активного питомца:\s*\n?(.*?)(?:\n\n[A-ZА-ЯЁ_ ]{4,}|\Z)",
        instructions or "",
        flags=re.DOTALL,
    )
    if not match:
        return ""
    value = match.group(1).strip()
    return value[:1200]


def _trim_input(response_input):
    if not isinstance(response_input, list):
        return response_input
    cleaned = []
    for item in response_input[-12:]:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("content"), str) and item.get("content") in {"[GENERAL_SCOPE]", "[PET_SCOPE]"}:
            continue
        cleaned.append(item)
    return cleaned


def _collect_urls(response, limit: int = 8):
    try:
        data = response.model_dump()
    except Exception:
        return []

    found = []
    seen = set()

    def walk(value):
        if len(found) >= limit:
            return
        if isinstance(value, dict):
            url = value.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                if url not in seen:
                    seen.add(url)
                    title = value.get("title") or value.get("name") or "Источник"
                    found.append((str(title)[:90], url))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return found


def _append_sources(answer: str, response) -> str:
    urls = _collect_urls(response)
    extras = [(title, url) for title, url in urls if url not in answer]
    if not extras:
        return answer
    lines = [answer.rstrip(), "", "Источники:"]
    for title, url in extras[:6]:
        lines.append(f"• {title}: {url}")
    return "\n".join(lines)


class _ResponseProxy:
    def __init__(self, response, output_text: str):
        self._response = response
        self.output_text = output_text

    def __getattr__(self, name):
        return getattr(self._response, name)


class _WebAwareResponses:
    def __init__(self, base_client):
        self._base_client = base_client
        api_key = os.getenv("OPENAI_API_KEY")
        self._web_client = OpenAI(api_key=api_key) if api_key else None

    def create(self, *args, **kwargs):
        response_input = kwargs.get("input")
        if not _needs_local_search(response_input) or self._web_client is None:
            return self._base_client.responses.create(*args, **kwargs)

        instructions = LOCAL_SEARCH_RULES
        if _dialog_scope(response_input) == "pet":
            pet_context = _active_pet_context(kwargs.get("instructions") or "")
            if pet_context:
                instructions += (
                    "\n\nСОХРАНЁННЫЕ ДАННЫЕ ТЕКУЩЕГО ПАЦИЕНТА:\n" + pet_context +
                    "\nИспользуй их только вместе с текущей клинической перепиской, чтобы понять, "
                    "какие возможности клиники нужны именно этому пациенту."
                )

        response = self._web_client.responses.create(
            model=kwargs.get("model", "gpt-5.6-sol"),
            instructions=instructions,
            input=_trim_input(response_input),
            tools=[{"type": "web_search", "search_context_size": "medium"}],
        )
        answer = (response.output_text or "").strip()
        return _ResponseProxy(response, _append_sources(answer, response))

    def __getattr__(self, name):
        return getattr(self._base_client.responses, name)


class _WebAwareClient:
    def __init__(self, base_client):
        self._base_client = base_client
        self.responses = _WebAwareResponses(base_client)

    def __getattr__(self, name):
        return getattr(self._base_client, name)


def install(bot):
    if bot.client is not None:
        bot.client = _WebAwareClient(bot.client)
