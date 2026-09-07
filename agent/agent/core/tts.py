import asyncio
import base64
import io
import json
import wave
from pathlib import Path

from piper import PiperVoice
from piper.download_voices import download_voice
from websockets.exceptions import ConnectionClosed

from agent.core.logging_util import log_event

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


async def speak(websocket, text: str) -> None:
    """Synthesize text and send it as a "speech" message. Used both for the
    agent's final replies and for spoken confirmation prompts -- shared here
    (rather than living in server.py) so dispatcher.py can call it too
    without a circular import. Failures are swallowed: TTS is additive,
    never a dependency of the actual response/confirmation it accompanies.
    """
    if websocket is None or not text:
        return
    try:
        loop = asyncio.get_running_loop()
        audio_bytes = await loop.run_in_executor(None, synthesize, text)
        await websocket.send(
            json.dumps({"type": "speech", "data": base64.b64encode(audio_bytes).decode()})
        )
    except ConnectionClosed:
        return
    except Exception as e:
        log_event("tts_error", error=str(e))
