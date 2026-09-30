import asyncio
import json

from websockets.exceptions import ConnectionClosed

from agent.core import audio_io, tts_backends, voice_settings
from agent.core.logging_util import log_event

_piper = tts_backends.BACKENDS[tts_backends.PiperBackend.name]


def synthesize(text: str) -> bytes:
    """Return WAV audio bytes for the given text, synthesized locally with
    Piper's default voice. Not the reply path (that's _synthesize_reply, which
    honours the selected backend); kept for scripts/voice_sim.py, which
    stitches its test utterances from the project's own voice."""
    return _piper.synthesize_wav(text)


def availability() -> dict:
    """{backend: usable}, booleans only -- see tts_backends.availability()."""
    return tts_backends.availability()


def _synthesize_reply(text: str) -> tuple:
    """(samples, rate, backend name) using the backend and voice the voice
    settings name, read per utterance so a change applies to the next reply.

    If a non-Piper backend fails -- no key, network down, an HTTP error, a
    timeout -- the failure is logged and this utterance falls back to Piper's
    default voice: a premium voice must never be the reason a reply is
    silent. A Piper failure propagates; speak() logs it and stays silent.
    """
    settings = voice_settings.load()
    name = settings.get("tts_backend") or tts_backends.DEFAULT_BACKEND
    voice = settings.get("tts_voice")
    backend = tts_backends.BACKENDS.get(name)
    if backend is None:
        log_event("tts_error", backend=name, error="unknown backend", fallback="piper")
        backend, voice = _piper, None
    try:
        audio, rate = backend.synthesize(text, voice)
        return audio, rate, backend.name
    except Exception as e:
        # A Piper voice that isn't the default gets the same treatment: the
        # default voice is the floor every other choice falls back to.
        if backend is _piper and not voice:
            raise
        log_event("tts_error", backend=backend.name, voice=voice, error=str(e), fallback="piper")
    audio, rate = _piper.synthesize(text)
    return audio, rate, _piper.name


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
            audio, rate, backend = await loop.run_in_executor(None, _synthesize_reply, text)
            duration_ms = int(len(audio) * 1000 / rate)
            await _send(
                websocket,
                {"type": "speaking", "state": "started", "text": text, "duration_ms": duration_ms, "backend": backend},
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
