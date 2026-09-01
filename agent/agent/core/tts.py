import io
import wave
from pathlib import Path

from piper import PiperVoice
from piper.download_voices import download_voice

VOICE_NAME = "en_US-amy-medium"
VOICE_DIR = Path(__file__).resolve().parent.parent / "assets" / "voices"
MODEL_PATH = VOICE_DIR / f"{VOICE_NAME}.onnx"
CONFIG_PATH = VOICE_DIR / f"{VOICE_NAME}.onnx.json"

_voice = None


def _get_voice() -> PiperVoice:
    global _voice
    if _voice is None:
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        if not MODEL_PATH.exists() or not CONFIG_PATH.exists():
            download_voice(VOICE_NAME, VOICE_DIR)
        _voice = PiperVoice.load(MODEL_PATH, CONFIG_PATH)
    return _voice


def synthesize(text: str) -> bytes:
    """Return WAV audio bytes for the given text, synthesized locally with Piper."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        _get_voice().synthesize_wav(text, wav_file)
    return buffer.getvalue()
