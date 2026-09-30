import asyncio
import io
import json
import wave
from pathlib import Path

from piper import PiperVoice
from piper.download_voices import download_voice
from websockets.exceptions import ConnectionClosed

from agent.core import audio_io
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


# Playback is swappable so a test harness (scripts/voice_sim.py) can drive the
# whole speaking path -- synthesis, `speaking` messages, capture muting --
# without sound coming out of the user's speakers.
_player = audio_io.play_blocking

# Told when speech starts and stops so the voice session can stop listening
# while Leutheria talks (half-duplex). Anything with speech_begin(),
# playback_started() and speech_end() methods; set by voice_session.
_observer = None

# One voice at a time. sounddevice.play() drives a single module-global
# stream, so two overlapping replies (a typed command and a voice one
# finishing together) would cut each other off mid-word instead of queueing.
_playback_lock = None


def set_player(player) -> None:
    global _player
    _player = player


def set_observer(observer) -> None:
    global _observer
    _observer = observer


def _notify(event: str) -> None:
    if _observer is None:
        return
    try:
        getattr(_observer, event)()
    except Exception as e:  # an observer bug must not take the reply down with it
        log_event("tts_error", error=f"observer {event}: {e}")


async def _send(websocket, message: dict) -> None:
    if websocket is None:
        return
    try:
        await websocket.send(json.dumps(message))
    except ConnectionClosed:
        pass


async def speak(websocket, text: str) -> None:
    """Synthesize text and play it through the Mac's speakers, bracketed by
    `speaking` started/ended messages on the given connection. Used both for
    the agent's final replies and for spoken confirmation prompts -- shared
    here (rather than living in server.py) so dispatcher.py can call it too
    without a circular import.

    The sidecar plays audio itself rather than shipping it to the renderer
    because an always-listening mic has to know exactly when the assistant
    is talking, and only the process driving the speaker knows that. It also
    means replies are heard with every window hidden, and with no UI
    connected at all (websocket=None still speaks).

    Returns once playback has finished. Failures are swallowed: TTS is
    additive, never a dependency of the actual response/confirmation it
    accompanies.
    """
    global _playback_lock
    if not text:
        return
    if _playback_lock is None:
        _playback_lock = asyncio.Lock()

    async with _playback_lock:
        _notify("speech_begin")
        started = False
        try:
            loop = asyncio.get_running_loop()
            audio_bytes = await loop.run_in_executor(None, synthesize, text)
            audio, rate = audio_io.wav_bytes_to_array(audio_bytes)
            duration_ms = int(len(audio) * 1000 / rate)
            await _send(
                websocket,
                {"type": "speaking", "state": "started", "text": text, "duration_ms": duration_ms},
            )
            started = True
            _notify("playback_started")
            await loop.run_in_executor(None, _player, audio, rate)
        except Exception as e:
            log_event("tts_error", error=str(e))
        finally:
            if started:
                await _send(websocket, {"type": "speaking", "state": "ended"})
            _notify("speech_end")
