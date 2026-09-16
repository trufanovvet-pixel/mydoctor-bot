from telegram import Message


MAX_CHUNK = 3900
_installed = False
_original_reply_text = None


def _split_text(text: str, limit: int = MAX_CHUNK):
    value = str(text or "")
    if len(value) <= limit:
        return [value]

    chunks = []
    rest = value
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit

        chunk = rest[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        rest = rest[cut:].lstrip()

    if rest:
        chunks.append(rest)
    return chunks or [value[:limit]]


def install():
    global _installed, _original_reply_text
    if _installed:
        return

    _original_reply_text = Message.reply_text

    async def safe_reply_text(self, text, *args, **kwargs):
        chunks = _split_text(text)
        if len(chunks) == 1:
            return await _original_reply_text(self, chunks[0], *args, **kwargs)

        result = None
        final_markup = kwargs.get("reply_markup")
        for index, chunk in enumerate(chunks):
            part_kwargs = dict(kwargs)
            if index < len(chunks) - 1:
                part_kwargs.pop("reply_markup", None)
            elif final_markup is not None:
                part_kwargs["reply_markup"] = final_markup
            result = await _original_reply_text(self, chunk, *args, **part_kwargs)
        return result

    Message.reply_text = safe_reply_text
    _installed = True
