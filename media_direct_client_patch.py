import os

from openai import OpenAI


def _has_media(response_input) -> bool:
    if not isinstance(response_input, list):
        return False
    for message in response_input:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"input_image", "input_file"}:
                return True
    return False


class _MediaSafeResponses:
    def __init__(self, base_client, direct_client):
        self._base_client = base_client
        self._direct_client = direct_client

    def create(self, *args, **kwargs):
        if _has_media(kwargs.get("input")):
            print("media direct client: bypassing text/web response wrappers", flush=True)
            return self._direct_client.responses.create(*args, **kwargs)
        return self._base_client.responses.create(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._base_client.responses, name)


class _MediaSafeClient:
    def __init__(self, base_client, direct_client):
        self._base_client = base_client
        self.responses = _MediaSafeResponses(base_client, direct_client)

    def __getattr__(self, name):
        return getattr(self._base_client, name)


def install(bot):
    api_key = os.getenv("OPENAI_API_KEY")
    if bot.client is not None and api_key:
        bot.client = _MediaSafeClient(bot.client, OpenAI(api_key=api_key))
