"""Swappable text-to-speech engines.

ElevenLabs is the default cloud voice (the user's own API key); macOS `say`
is the local floor every other engine falls back to; Piper is an optional
offline voice that is only there if `requirements-piper.txt` was installed --
it is GPL-3.0, so a distributed build must work without it (NOTES.md #1).
All hand back plain int16 samples plus a sample rate, so tts.py's playback,
`speaking` messages and half-duplex muting never know which engine produced
the audio.

Kept free of heavy imports (Piper pulls in onnxruntime) so control.py can call
availability() for the Voice view without loading a voice model -- same stance
as voice_settings.py. Piper is never imported here at module level: a missing
`piper` package makes that backend unavailable, never an ImportError at startup.
"""

import importlib.util
import os
import re
import subprocess
import tempfile
import threading
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np
import requests

# A voice id ends up in a file path (Piper) or a URL path (ElevenLabs), so it
# is restricted to characters that can't escape either. Piper names
# ("en_US-amy-medium") and ElevenLabs ids ("JBFqnCBsd6RMkjVDRZzb") both fit.
VOICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}\Z")


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
    """Optional: only present when `piper-tts` (GPL-3.0) is installed."""

    name = "piper"
    DEFAULT_VOICE = "en_US-amy-medium"
    VOICE_DIR = Path(__file__).resolve().parent.parent / "assets" / "voices"
    MODULE = "piper"  # a test blocks this to simulate a build without Piper

    def __init__(self):
        self._voices = {}  # name -> loaded PiperVoice
        self._failed = {}  # name -> reason; a bad name isn't re-downloaded every reply

    def available(self) -> bool:
        # find_spec locates the package without importing it (no onnxruntime
        # load for a settings read). A blocked or absent module is None.
        try:
            return importlib.util.find_spec(self.MODULE) is not None
        except (ImportError, ValueError):
            return False

    def _load(self, name: str):
        if name in self._voices:
            return self._voices[name]
        if name in self._failed:
            raise TTSError(self._failed[name])
        # The name becomes a file name under VOICE_DIR; voice settings accept
        # wider ids (macOS voice names), so Piper checks its own.
        if not VOICE_ID_PATTERN.match(name):
            raise TTSError(f"invalid piper voice name {name!r}")
        try:
            from piper import PiperVoice
            from piper.download_voices import download_voice
        except ImportError:
            raise TTSError("piper is not installed (see requirements-piper.txt)") from None
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


class MacSayBackend(TTSBackend):
    """macOS's built-in `say`, rendered to a file -- never played by `say`.

    The local fallback: it ships with every Mac, needs no download, no key
    and no GPL component. `say -o` writes 16-bit mono WAV into a private
    temp directory, which is read back into samples and removed; playback
    stays in tts.speak() like every other engine, so half-duplex muting is
    unchanged. The argv is fixed: the text goes in on stdin (`-f -`), so a
    reply starting with "-" can't become an option, and a voice name is only
    passed after it matched a name `say -v '?'` listed.
    """

    name = "say"
    DEFAULT_VOICE = None  # the system voice (System Settings > Spoken Content)
    SAY = "/usr/bin/say"
    SAMPLE_RATE = 22050
    TIMEOUT_S = 60  # a long reply renders in a few seconds; this is a hang guard
    LIST_TIMEOUT_S = 10
    # "Samantha", "Eddy (English (US))", accented names: letters, digits,
    # spaces, parentheses, _ and -. Never a path separator or a dot.
    NAME_PATTERN = re.compile(r"^\w[\w() -]{0,99}\Z")

    def __init__(self, run=None):
        self._run = run or subprocess.run  # injectable for scripts/tts_check.py
        self._voice_cache = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return os.path.exists(self.SAY)

    def voices(self) -> frozenset:
        """Installed voice names, from `say -v '?'`, listed once per process.
        A failed listing isn't cached, so a transient failure can retry."""
        with self._lock:
            if self._voice_cache is not None:
                return self._voice_cache
        result = self._run([self.SAY, "-v", "?"], capture_output=True, text=True, timeout=self.LIST_TIMEOUT_S)
        if result.returncode != 0:
            raise TTSError(f"say -v '?' failed (exit {result.returncode})")
        names = set()
        for line in result.stdout.splitlines():
            # "Eddy (English (US)) en_US    # Hello! My name is Eddy."
            match = re.match(r"^(.*?)\s+[a-z]{2,3}[_-][A-Za-z0-9]+\s+#", line)
            if match and self.NAME_PATTERN.match(match.group(1)):
                names.add(match.group(1))
        with self._lock:
            self._voice_cache = frozenset(names)
        return self._voice_cache

    def _check_voice(self, voice: str) -> None:
        if not isinstance(voice, str) or not self.NAME_PATTERN.match(voice):
            raise TTSError(f"invalid macOS voice name {voice!r}")
        if voice not in self.voices():
            raise TTSError(f"macOS voice {voice!r} is not installed")

    def synthesize_wav(self, text: str, voice: str = None) -> bytes:
        if not self.available():
            raise TTSError(f"{self.SAY} not found")
        argv = [self.SAY]
        if voice:
            self._check_voice(voice)
            argv += ["-v", voice]
        with tempfile.TemporaryDirectory(prefix="leutheria-tts-") as tmp:  # 0700, removed on exit
            out = Path(tmp) / "reply.wav"
            argv += ["-o", str(out), "--file-format=WAVE", f"--data-format=LEI16@{self.SAMPLE_RATE}", "-f", "-"]
            try:
                result = self._run(argv, input=text, capture_output=True, text=True, timeout=self.TIMEOUT_S)
            except subprocess.TimeoutExpired:
                raise TTSError(f"say timed out after {self.TIMEOUT_S}s") from None
            except OSError as e:
                raise TTSError(f"say could not start: {type(e).__name__}") from None
            if result.returncode != 0:
                raise TTSError(f"say failed (exit {result.returncode}): {(result.stderr or '').strip()[:200]}")
            try:
                data = out.read_bytes()
            except OSError:
                raise TTSError("say wrote no audio file") from None
        if len(data) <= 44:  # a WAV header and nothing after it
            raise TTSError("say produced empty audio")
        return data

    def synthesize(self, text: str, voice: str = None) -> tuple:
        from agent.core import audio_io

        try:
            audio, rate = audio_io.wav_bytes_to_array(self.synthesize_wav(text, voice))
        except TTSError:
            raise
        except Exception as e:  # a malformed file from say
            raise TTSError(f"say output unreadable: {e}") from None
        if not len(audio):
            raise TTSError("say produced empty audio")
        return audio, rate


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


BACKENDS = {backend.name: backend for backend in (MacSayBackend(), PiperBackend(), ElevenLabsBackend())}
DEFAULT_BACKEND = ElevenLabsBackend.name
FALLBACK_BACKEND = MacSayBackend.name  # what any other engine falls back to, per utterance (tts.py)


def availability() -> dict:
    """{backend name: usable} for the Voice view. Booleans only -- whether a
    key exists, never the key."""
    return {name: backend.available() for name, backend in BACKENDS.items()}
