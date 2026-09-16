class _TranscriptionsFallback:
    def __init__(self, base):
        self._base = base

    def create(self, *args, **kwargs):
        try:
            return self._base.create(*args, **kwargs)
        except Exception as exc:
            print(f"voice transcription primary error: {exc!r}", flush=True)
            file_obj = kwargs.get("file")
            try:
                if hasattr(file_obj, "seek"):
                    file_obj.seek(0)
            except Exception:
                pass
            fallback = dict(kwargs)
            fallback["model"] = "whisper-1"
            return self._base.create(*args, **fallback)


class _AudioFallback:
    def __init__(self, base_audio):
        self._base = base_audio
        self.transcriptions = _TranscriptionsFallback(base_audio.transcriptions)

    def __getattr__(self, name):
        return getattr(self._base, name)


class _ClientWithVoiceFallback:
    def __init__(self, base_client):
        self._base = base_client
        self.audio = _AudioFallback(base_client.audio)

    def __getattr__(self, name):
        return getattr(self._base, name)


def install(bot):
    if bot.client is not None:
        bot.client = _ClientWithVoiceFallback(bot.client)
