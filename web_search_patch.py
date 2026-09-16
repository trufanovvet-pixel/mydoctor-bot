import os

from openai import OpenAI


LOCAL_SEARCH_RULES = """

РЕЖИМ АКТУАЛЬНОГО ПОИСКА ВЕТЕРИНАРНЫХ КЛИНИК
Пользователь просит найти ветеринарную клинику, врача или ветеринарную помощь по месту.
Это навигационный запрос, а не новый этап клинического опроса.

Обязательно используй веб-поиск и актуальные публичные источники.

Если в текущем сообщении или недавней переписке НЕ указаны город, район, адрес,
ориентир или координаты, а пользователь пишет «рядом со мной», «ближайшую»,
«у меня в городе» и т.п. — не угадывай местоположение и не отказывайся от поиска.
Коротко спроси: «Напишите город/район или ближайший ориентир — найду варианты рядом».

Когда местоположение известно:
- найди обычно 3–5 реально существующих ветеринарных клиник поблизости;
- для каждой укажи название, адрес, рейтинг и число отзывов ТОЛЬКО если это удалось
  подтвердить актуальным источником, часы работы, телефон и официальный сайт, если они есть;
- если официальный сайт не найден, так и напиши, не подменяй его случайным каталогом;
- добавь удобную ссылку Google Maps для поиска клиники по названию и адресу;
- если пользователь просит круглосуточную/экстренную помощь, отдавай приоритет клиникам,
  для которых удалось подтвердить работу 24/7 или текущие часы;
- не называй точное расстояние или время в пути, если источник этого не подтверждает;
- не придумывай рейтинг, отзывы, часы, телефон, адрес или сайт;
- если данные в источниках расходятся, кратко отметь это;
- в срочной ситуации посоветуй перед выездом позвонить и подтвердить, что клиника принимает.

Формат ответа — компактный русский текст, удобный для Telegram. Для каждого варианта
отдельный короткий блок. URL пиши полностью, чтобы Telegram сделал их кликабельными.
"""


LOCAL_TERMS = (
    "ветклиник", "ветеринарная клиник", "ветеринарную клиник", "ветеринарной клиник",
    "ветцентр", "вет центр", "ветеринарный центр", "ветеринарную помощь",
    "ветврач", "вет врач", "ветеринар рядом", "круглосуточная клиник",
)

SEARCH_TERMS = (
    "найди", "найдите", "найти", "ближайш", "рядом", "поблизости", "недалеко",
    "куда обратиться", "куда ехать", "рейтинг", "сайт клиники", "адрес клиники",
    "открыта сейчас", "работает сейчас", "круглосуточ",
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
    value = _plain_text(response_input).lower().replace("ё", "е")
    if not value:
        return False
    return (
        any(term in value for term in LOCAL_TERMS)
        and any(term in value for term in SEARCH_TERMS)
    )


def _trim_input(response_input):
    if not isinstance(response_input, list):
        return response_input
    cleaned = []
    for item in response_input[-10:]:
        if not isinstance(item, dict):
            continue
        if item.get("content") in {"[GENERAL_SCOPE]", "[PET_SCOPE]"}:
            continue
        cleaned.append(item)
    return cleaned


def _collect_urls(response, limit: int = 6):
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
    for title, url in extras[:5]:
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
        if not _needs_local_search(kwargs.get("input")) or self._web_client is None:
            return self._base_client.responses.create(*args, **kwargs)

        response = self._web_client.responses.create(
            model=kwargs.get("model", "gpt-5.6-sol"),
            instructions=LOCAL_SEARCH_RULES,
            input=_trim_input(kwargs.get("input")),
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
