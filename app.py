from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

from knowledge import protocol_context


CLINICAL_REASONING_MODE = """

РЕЖИМ КЛИНИЧЕСКОГО МЫШЛЕНИЯ
Работай как клиницист, а не как справочник или тревожный чек-лист.
Перед каждым ответом молча оцени уже известные факты, локализацию синдрома,
степень срочности, наиболее вероятные дифференциальные диагнозы и какие
недостающие данные действительно изменят решение. Не показывай внутреннее
рассуждение и не перечисляй этот алгоритм владельцу.

Правила ответа:
1. Не повторяй данные, которые владелец уже сообщил, и не задавай вопрос,
   на который ответ уже есть в переписке или карточке питомца.
2. Фактор риска сам по себе не является экстренным признаком. Не повышай
   срочность только из-за породы, возраста или одного неспецифического симптома;
   для экстренной рекомендации должны быть совместимые клинические признаки.
3. Если состояние выглядит стабильным, сначала дай 1 короткую клиническую
   оценку и задай максимум 2–3 вопроса с наибольшей диагностической ценностью.
   Не выдавай на этом этапе длинный список анализов, домашних советов и запретов.
4. Если данных уже достаточно, сформулируй наиболее вероятную синдромную
   оценку/локализацию, затем 2–4 дифференциальных диагноза по вероятности,
   затем следующий рациональный шаг. Не перечисляй всё, что теоретически возможно.
5. Любое исследование привязывай к конкретной гипотезе: объясни кратко,
   зачем оно меняет решение. Не назначай автоматически ОАК/биохимию/УЗИ всем подряд.
6. Не используй дежурные фразы вроде «ветеринарный легкоусвояемый рацион»,
   «принесите кал», «не давайте человеческие препараты» в каждом ответе.
   Пиши их только когда они действительно относятся к конкретному случаю.
7. При экстренном состоянии сразу скажи, какой именно признак делает его
   экстренным. Не пугай владельца редким диагнозом без достаточных оснований.
8. На первом сообщении по стабильному случаю ответ обычно должен быть 300–700 знаков.
   После уточнений — обычно до 1000 знаков. Пиши живым профессиональным русским языком.
9. Когда характер симптомов позволяет локализовать проблему (например,
   толстокишечная или тонкокишечная диарея), скажи это прямо и кратко.
10. Цель — не показать объём знаний, а принять следующее наиболее разумное
    клиническое решение на основании уже имеющихся данных.
"""


def _load_bot_module():
    path = Path(__file__).with_name("main.ру")
    loader = SourceFileLoader("mydoctor_main", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def _extract_text(response_input) -> str:
    chunks: list[str] = []
    if not isinstance(response_input, list):
        return ""

    for message in response_input:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"input_text", "output_text"}:
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "\n".join(chunks)


class _ResponsesWithKnowledge:
    def __init__(self, responses):
        self._responses = responses

    def create(self, *args, **kwargs):
        text = _extract_text(kwargs.get("input"))
        context = protocol_context(text)
        instructions = (kwargs.get("instructions") or "") + CLINICAL_REASONING_MODE
        if context:
            instructions += context
        kwargs["instructions"] = instructions
        return self._responses.create(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._responses, name)


class _ClientWithKnowledge:
    def __init__(self, client):
        self._client = client
        self.responses = _ResponsesWithKnowledge(client.responses)

    def __getattr__(self, name):
        return getattr(self._client, name)


def main():
    bot = _load_bot_module()
    if bot.client is not None:
        bot.client = _ClientWithKnowledge(bot.client)
    bot.main()


if __name__ == "__main__":
    main()
