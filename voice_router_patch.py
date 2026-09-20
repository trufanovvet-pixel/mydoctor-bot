import asyncio
import io


class _MessageProxy:
    def __init__(self, base, text: str):
        self._base = base
        self.text = text
        self.voice = None

    def __getattr__(self, name):
        return getattr(self._base, name)


class _UpdateProxy:
    def __init__(self, base, text: str):
        self._base = base
        self.message = _MessageProxy(base.message, text)

    @property
    def effective_message(self):
        return self.message

    def __getattr__(self, name):
        return getattr(self._base, name)


def install(bot):
    async def routed_voice_message(update, context):
        await bot.ensure_current_user(update)
        if bot.client is None:
            await update.message.reply_text("ИИ временно не подключён.", reply_markup=bot.MENU)
            return

        voice = update.message.voice
        if not voice:
            return
        if (voice.file_size or 0) > 20 * 1024 * 1024:
            await update.message.reply_text("Голосовое слишком большое. Пришлите более короткое сообщение.", reply_markup=bot.MENU)
            return

        await update.message.chat.send_action("typing")
        try:
            tg_file = await context.bot.get_file(voice.file_id)
            audio_bytes = bytes(await tg_file.download_as_bytearray())
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "voice.ogg"
            transcription = await asyncio.to_thread(
                bot.client.audio.transcriptions.create,
                model="gpt-4o-mini-transcribe",
                file=audio_file,
                language="ru",
            )
            text = (transcription.text or "").strip()
            if not text:
                raise ValueError("empty transcription")
            print(f"voice transcribed chars={len(text)}", flush=True)
        except Exception as exc:
            print(f"voice router transcription error: {exc!r}", flush=True)
            await update.message.reply_text(
                "Не получилось распознать голосовое. Попробуйте записать ещё раз.",
                reply_markup=bot.MENU,
            )
            return

        # Important: after transcription, use exactly the same routing as typed text.
        # This lets natural-language calendar commands, consultation flows and normal
        # questions behave identically for text and voice.
        proxy = _UpdateProxy(update, text)
        await bot.message(proxy, context)

    bot.voice_message = routed_voice_message
