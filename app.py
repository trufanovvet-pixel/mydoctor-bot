from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

from knowledge import protocol_context


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
        if context:
            kwargs["instructions"] = (kwargs.get("instructions") or "") + context
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
