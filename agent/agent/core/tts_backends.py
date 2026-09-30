"""Swappable text-to-speech engines.

Piper is the free, offline default; ElevenLabs is an opt-in cloud voice that
needs the user's own API key. Both hand back plain int16 samples plus a sample
rate, so tts.py's playback, `speaking` messages and half-duplex muting never
know which engine produced the audio.

Kept free of heavy imports (Piper pulls in onnxruntime) so control.py can call
availability() for the Voice view without loading a voice model -- same stance
as voice_settings.py.
"""

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np
import requests

# A voice id ends up in a file path (Piper) or a URL path (ElevenLabs), so it
# is restricted to characters that can't escape either. Piper names
# ("en_US-amy-medium") and ElevenLabs ids ("JBFqnCBsd6RMkjVDRZzb") both fit.
VOICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class TTSError(Exception):
    """A synthesis failure with a reason fit for the log. Never carries a key."""


class TTSBackend(ABC):
    name: str = ""

    @abstractmethod
    def synthesize(self, text: str, voice: str = None) -> tuple:
        """Return (int16 numpy samples, sample rate) for the text.

        This is the seam tts.py plays from -- callers only ever depend on this
        interface. `voice` is backend-specific (a Piper voice name, an
        ElevenLabs voice id); None means the backend's default. Raise TTSError
        (or anything) on failure; tts.py decides what to fall back to.
        Blocking: tts.py runs it in an executor.
        """

    def available(self) -> bool:
        """Whether this backend is configured well enough to try. A True here
        is not a promise the next call succeeds (the network can still fail)."""
        return True


class PiperBackend(TTSBackend):
    name = "piper"
    DEFAULT_VOICE = "en_US-amy-medium"
    VOICE_DIR = Path(__file__).resolve().parent.parent / "assets" / "voices"

    def __init__(self):
        self._voices = {}  # name -> loaded PiperVoice
        self._failed = {}  # name -> reason; a bad name isn't re-downloaded every reply

    def _load(self, name: str):
        if name in self._voices:
            return self._voices[name]
        if name in self._failed:
            raise TTSError(self._failed[name])
        from piper import PiperVoice
        from piper.download_voices import download_voice

        model = self.VOICE_DIR / f"{name}.onnx"
        config = self.VOICE_DIR / f"{name}.onnx.json"
        try:
            self.VOICE_DIR.mkdir(parents=True, exist_ok=True)
            if not model.exists() or not config.exists():
                download_voice(name, self.VOICE_DIR)
            voice = PiperVoice.load(model, config)
        except Exception as e:
            # Only a non-default name is remembered as bad: the default voice
            # failing is likely transient (offline on first run) and must retry.
            if name != self.DEFAULT_VOICE:
                self._failed[name] = f"piper voice {name!r} unavailable: {e}"
                raise TTSError(self._failed[name]) from e
            raise
        self._voices[name] = voice
        return voice

    def synthesize_wav(self, text: str, voice: str = None) -> bytes:
        """WAV bytes -- what the voice simulator stitches test utterances from."""
        import io
        import wave

        # Load before opening the writer: a wave writer closed with no frames
        # raises "# channels not specified" from __exit__, which replaced the
        # real load error (offline download, bad voice name) in the log.
        piper_voice = self._load(voice or self.DEFAULT_VOICE)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            piper_voice.synthesize_wav(text, wav_file)
        return buffer.getvalue()

    def synthesize(self, text: str, voice: str = None) -> tuple:
        from agent.core import audio_io

        return audio_io.wav_bytes_to_array(self.synthesize_wav(text, voice))


class ElevenLabsBackend(TTSBackend):
    """ElevenLabs text-to-speech REST API (POST /v1/text-to-speech/{voice_id}).

    Asks for raw PCM (signed 16-bit little-endian, mono) so there's nothing to
    decode -- no mp3 dependency. 24 kHz because 44.1 kHz PCM needs a Pro plan
    and every rate plays the same through sounddevice.
    """

    name = "elevenlabs"
    BASE_URL = "https://api.elevenlabs.io"
    # "George", the voice the API reference uses in its examples -- a
    # premade voice every account can use.
    DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"
    # Flash: ~75 ms model latency and half the per-character price of the
    # multilingual model. A spoken reply is latency-bound, not quality-bound.
    MODEL_ID = "eleven_flash_v2_5"
    SAMPLE_RATE = 24000
    ENV_KEY = "ELEVENLABS_API_KEY"

    def __init__(self, base_url: str = None, timeout: tuple = (5, 20)):
        # base_url is injectable for scripts/tts_check.py's fake server.
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.timeout = timeout  # (connect, read) seconds; every request has one
        self.model_id = os.environ.get("ELEVENLABS_MODEL_ID") or self.MODEL_ID

    def _key(self) -> str:
        # Read per call rather than at import: the .env is loaded by
        # __main__.py, and a key added to the environment is picked up
        # without the backend object needing rebuilding.
        return (os.environ.get(self.ENV_KEY) or "").strip()

    def available(self) -> bool:
        return bool(self._key())

    def synthesize(self, text: str, voice: str = None) -> tuple:
        key = self._key()
        if not key:
            raise TTSError(f"no API key ({self.ENV_KEY} is not set)")
        voice_id = voice or self.DEFAULT_VOICE
        if not VOICE_ID_PATTERN.match(voice_id):
            raise TTSError(f"invalid voice id {voice_id!r}")
        try:
            response = requests.post(
                f"{self.base_url}/v1/text-to-speech/{voice_id}",
                params={"output_format": f"pcm_{self.SAMPLE_RATE}"},
                headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/pcm"},
                json={"text": text, "model_id": self.model_id},
                timeout=self.timeout,
            )
        except requests.Timeout:
            raise TTSError(f"request timed out after {self.timeout}s") from None
        except requests.RequestException as e:
            raise TTSError(f"request failed: {type(e).__name__}") from None
        if response.status_code != 200:
            raise TTSError(f"HTTP {response.status_code}: {_error_detail(response)}")
        if "json" in response.headers.get("Content-Type", ""):
            raise TTSError(f"expected audio, got JSON: {_error_detail(response)}")
        data = response.content
        if len(data) < 2:
            raise TTSError("empty audio")
        # An odd trailing byte would make frombuffer raise; drop it.
        audio = np.frombuffer(data[: len(data) - len(data) % 2], dtype="<i2").astype(np.int16)
        return audio, self.SAMPLE_RATE


def _error_detail(response) -> str:
    """The API's own message if it sent one (`{"detail": {"message": ...}}` or
    `{"detail": "..."}`), else a short slice of the body."""
    try:
        body = response.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("status") or detail
        return str(detail)[:200]
    except ValueError:
        return (response.text or "").strip()[:200] or "no detail"


BACKENDS = {backend.name: backend for backend in (PiperBackend(), ElevenLabsBackend())}
DEFAULT_BACKEND = ElevenLabsBackend.name  # falls back to Piper per utterance (tts.py)


def availability() -> dict:
    """{backend name: usable} for the Voice view. Booleans only -- whether a
    key exists, never the key."""
    return {name: backend.available() for name, backend in BACKENDS.items()}
