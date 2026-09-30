"""The always-listening voice session: wake word -> hands-free conversation
-> dismissal, all Python-side.

State machine (the contract is plans/week4-voice-spec.md):

    off -> idle                       voice enabled, mic open, wake word armed
    idle -> listening                 wake word, or a manual start
    listening -> hearing              VAD hears speech
    hearing -> transcribing           ~800 ms of trailing silence (endpoint)
    transcribing -> idle              a dismissal ("thanks, that's all")
    transcribing -> listening         nothing usable was said
    transcribing -> thinking          a command; runs through server.py
    thinking -> speaking -> listening the reply is played
    thinking -> speaking -> awaiting_confirmation   a spoken yes/no prompt
    listening -> idle                 silence_timeout_s with nothing said
    any -> idle                       manual end

Everything audio-shaped is injected: frames come from `source_factory` (the
mic in production, a WAV clip in scripts/voice_sim.py), commands go out
through `on_command`, and the session never touches a websocket itself --
it hands messages to `emit`. That keeps this module a pure state machine
over a stream of 80 ms frames, which is what makes it testable at all.

Timing inside a session (endpointing, minimum speech, the silence timeout)
is counted in frames, i.e. on the audio's own clock rather than the wall
clock, so a test clip behaves exactly like the same audio from a mic.
Only playback muting is wall-clock, because playback is.
"""

import asyncio
import contextlib
import math
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from agent.core import audio_io, stt, tts
from agent.core.logging_util import log_event

FRAME_MS = audio_io.FRAME_MS

VAD_THRESHOLD = 0.5
# Decision #5 of the spec: an utterance only reaches Whisper if VAD heard at
# least this much actual speech. Whisper's classic failure is to "hear"
# "Thank you." in a cough or a door closing -- which would dismiss the
# session -- and the cheapest guard is to never ask it about such audio.
MIN_SPEECH_MS = 300
# Segments Whisper itself thinks are probably not speech are dropped even if
# it produced text for them. Whisper's own default skips a segment only when
# no_speech_prob > 0.6 *and* its logprob is low; its confident-sounding
# "Thanks for watching!" hallucinations pass that, so here 0.6 alone decides.
NO_SPEECH_MAX = 0.6
# Dismissals must be short: "thanks" ends the session, "thanks, now open my
# calendar and find Thursday" does not.
DISMISS_MAX_WORDS = 5
MAX_UTTERANCE_MS = 30_000  # force an endpoint on someone who never pauses
PREROLL_FRAMES = 3  # VAD onset lags the first syllable; keep 240 ms before it
PLAYBACK_TAIL_S = 0.3  # room echo after the speaker stops

# Words that can ride along with a dismiss phrase without making it a
# request: "okay thanks", "great, that's all for now", "no that's it, bye".
_DISMISS_FILLER = {
    "ok", "okay", "oh", "great", "cool", "perfect", "awesome", "alright", "all", "right",
    "so", "very", "much", "a", "lot", "bye", "good", "for", "now", "then", "you", "and",
    "yeah", "yes", "no", "nope", "nothing", "else", "fine", "jarvis", "leutheria", "hey",
}

# Vocalizations, not requests. An open mic hears plenty of "hmm" and "uh"
# that clear the speech-duration gate, and Whisper dutifully spells them out
# ("HM") -- sent on, the model would earnestly reply to a thinking noise.
_NON_WORDS = {"hm", "hmm", "hmmm", "mm", "mmm", "mhm", "uh", "uhh", "um", "umm", "ah", "er", "erm", "oh"}

IN_SESSION_LISTENING ={"listening", "hearing", "awaiting_confirmation"}

_current = None


def current():
    """The live session object, or None if voice never started (control.py)."""
    return _current


def _words(text: str) -> list:
    return re.findall(r"[a-z0-9']+", text.lower().replace("’", "'"))


def is_dismissal(text: str, phrases: list) -> bool:
    """Is this utterance essentially just a dismiss phrase? A phrase has to be
    present, the whole thing has to be short, and everything left once the
    phrase is removed has to be filler -- so "thanks, what's the weather"
    (four words) is a request that happens to open politely, not a goodbye."""
    words = _words(text)
    if not words or len(words) > DISMISS_MAX_WORDS:
        return False
    remaining = " " + " ".join(words) + " "
    matched = False
    for phrase in sorted(phrases, key=len, reverse=True):
        normalized = " ".join(_words(phrase))
        if normalized and f" {normalized} " in remaining:
            matched = True
            remaining = remaining.replace(f" {normalized} ", " ")
    return matched and all(w in _DISMISS_FILLER for w in remaining.split())


def audio_level(frame: np.ndarray) -> float:
    """Frame loudness mapped to 0..1 on a dB scale (-60 dBFS -> 0, -10 -> 1),
    which tracks how loud speech sounds far better than raw RMS does."""
    rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2))) / 32768.0
    db = 20 * math.log10(rms + 1e-9)
    return min(1.0, max(0.0, (db + 60) / 50))


class OpenWakeWordDetector:
    """openWakeWord over ONNX. The tflite backend it defaults to has no wheel
    for Python 3.12 on macOS; ONNX runtime is already here for Piper."""

    def __init__(self, models: list):
        import openwakeword
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        resolved = []
        for name in models:
            if Path(name).expanduser().exists():
                resolved.append(str(Path(name).expanduser()))
                continue
            # Pretrained names download on first use, same as the Piper voice.
            download_models([name])
            if not any(name in Path(m["model_path"]).name for m in openwakeword.MODELS.values()):
                raise ValueError(f"unknown wake word model: {name}")
            resolved.append(name)
        self.model = Model(wakeword_models=resolved, inference_framework="onnx")

    def score(self, frame: np.ndarray) -> tuple:
        scores = self.model.predict(frame)
        name = max(scores, key=scores.get)
        return name, float(scores[name])

    def reset(self) -> None:
        self.model.reset()


class SileroVAD:
    """The Silero VAD that ships inside openWakeWord. Chosen over webrtcvad
    because it's a small neural model that tells speech from steady noise
    (fans, keyboard, music) far better than webrtcvad's energy/GMM approach,
    and it costs no extra dependency -- the model file comes with
    openWakeWord's download."""

    def __init__(self):
        from openwakeword.utils import download_models
        from openwakeword.vad import VAD

        # download_models always fetches the shared feature + VAD models; a
        # name that matches no wake model fetches only those. (An empty list
        # would mean "every pretrained wake model".)
        download_models(["__vad_only__"])
        self.vad = VAD()

    def speech_prob(self, frame: np.ndarray) -> float:
        return float(self.vad.predict(frame, frame_size=640))

    def reset(self) -> None:
        self.vad.reset_states()


class VoiceSession:
    def __init__(
        self,
        settings: dict,
        emit,
        on_command,
        on_confirmation_answer,
        confirmation_pending,
        on_dismiss_pending=None,
        source_factory=None,
        wake_factory=None,
        vad_factory=None,
        transcriber=None,
    ):
        """
        emit(message)                  -- async; delivers a status message to the UI
        on_command(text)               -- async; runs one voice command to completion
        on_confirmation_answer(text)   -- async -> bool; True if it answered the
                                          pending confirmation
        confirmation_pending()         -- is a confirmation waiting on the primary UI?
        on_dismiss_pending()           -- async; decline whatever is pending (the
                                          session was dismissed over an open question)
        source_factory(settings)       -- async iterator of int16 80 ms frames
        """
        self.settings = dict(settings)
        self._emit_fn = emit
        self._on_command = on_command
        self._on_confirmation_answer = on_confirmation_answer
        self._confirmation_pending = confirmation_pending
        self._on_dismiss_pending = on_dismiss_pending
        self._source_factory = source_factory or (lambda s: audio_io.mic_frames(s.get("input_device")))
        self._wake_factory = wake_factory or OpenWakeWordDetector
        self._vad_factory = vad_factory or SileroVAD
        self._transcriber = transcriber or stt.transcribe_segments

        self.state = "off"
        self.in_session = False
        self._sent_state = None
        self._session_started_at = 0.0

        self._wake = None
        self._vad = None
        # Resets are requested from the event loop but applied on the
        # inference thread, right before the next prediction, so a reset can
        # never race a prediction that's mid-flight on the same model.
        self._reset_wake = False
        self._reset_vad = False
        # Wake/VAD inference is a few ms per frame; one dedicated thread keeps
        # it off the event loop and keeps the (stateful) models single-threaded.
        self._infer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-infer")

        self._changed = asyncio.Event()
        self._restart_capture = False
        self._failed = False
        self._manual_start_pending = False
        self.source_done = asyncio.Event()  # a finite (test) source ran out

        self._outbox: asyncio.Queue = asyncio.Queue()
        self._tasks: set = set()

        # utterance being collected
        self._preroll: deque = deque(maxlen=PREROLL_FRAMES)
        self._utterance: list = []
        self._speech_frames = 0
        self._trailing_silence = 0
        self._resume_state = "listening"
        self._silence_frames = 0  # frames of `listening` with nothing said
        self._level = 0.0

        # playback / command bookkeeping
        self._speech_busy = 0
        self._mute_until = 0.0
        self._command_task = None
        self._pending_polls = 0

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        global _current
        _current = self
        tts.set_observer(self)
        self._spawn(self._sender())
        self._spawn(self._supervise())

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._infer.shutdown(wait=False)

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def snapshot(self) -> dict:
        return {"type": "voice_state", "state": self.state, "session": self.in_session}

    # -- control surface (called synchronously from control.py) ----------------

    def request(self, action: str) -> dict:
        if action == "start":
            if self.in_session:
                return {"ok": True, "state": self.state, "session": True}
            self._failed = False
            if self.state == "off":
                # Voice is disabled (or the mic failed): open it just for this
                # session. On session end it goes back to off.
                self._manual_start_pending = True
                self._changed.set()
            else:
                self._begin_session("manual")
            return {"ok": True, "state": self.state, "session": self.in_session or self._manual_start_pending}
        if action == "end":
            self._manual_start_pending = False
            if self.in_session:
                self._end_session("manual")
            return {"ok": True, "state": self.state, "session": False}
        return {"ok": False, "error": f"unknown voice_session action: {action}"}

    def apply_settings(self, settings: dict) -> None:
        old = self.settings
        self.settings = dict(settings)
        # Any change is a retry after a failure -- the usual fix for "voice
        # went off" is exactly a settings change (a model name, a device).
        self._failed = False
        if old.get("wake_models") != settings.get("wake_models"):
            self._wake = None
            self._restart_capture = True
        if old.get("input_device") != settings.get("input_device"):
            self._restart_capture = True
        if not settings.get("enabled") and old.get("enabled") and self.in_session:
            self._end_session("manual")
        self._changed.set()

    # -- state + messages --------------------------------------------------------

    def _emit(self, message: dict) -> None:
        self._outbox.put_nowait(message)

    async def _sender(self):
        # One sender drains an ordered queue, so the UI always sees transitions
        # in the order they happened even though they're raised from sync code.
        while True:
            message = await self._outbox.get()
            try:
                await self._emit_fn(message)
            except Exception as e:
                log_event("voice_error", stage="emit", error=str(e))

    def _set_state(self, state: str) -> None:
        self.state = state
        key = (state, self.in_session)
        if key != self._sent_state:
            self._sent_state = key
            self._emit(self.snapshot())

    def _capture_wanted(self) -> bool:
        if self._failed:
            return False
        return bool(self.settings.get("enabled") or self.in_session or self._manual_start_pending)

    def _begin_session(self, trigger: str, wake_word=None, score=None) -> None:
        self.in_session = True
        self._manual_start_pending = False
        self._session_started_at = time.monotonic()
        self._silence_frames = 0
        self._reset_utterance()
        self._reset_vad = True
        self._emit({"type": "session_started", "trigger": trigger, "wake_word": wake_word, "score": score})
        log_event("voice_session_started", trigger=trigger, wake_word=wake_word, score=score)
        self._set_state("listening")

    def _end_session(self, reason: str) -> None:
        if not self.in_session:
            return
        self.in_session = False
        self._reset_utterance()
        # Clear the detector's rolling buffer so the audio that opened this
        # session (or anything since) can't immediately reopen it.
        self._reset_wake = True
        self._emit({"type": "session_ended", "reason": reason})
        log_event(
            "voice_session_ended",
            reason=reason,
            duration_s=round(time.monotonic() - self._session_started_at, 1),
        )
        self._set_state("idle" if self._capture_wanted() else "off")
        self._changed.set()

    def _fail(self, stage: str, error: Exception) -> None:
        log_event("voice_error", stage=stage, error=str(error))
        self._failed = True
        self._manual_start_pending = False
        if self.in_session:
            self._end_session("error")
        self._set_state("off")

    # -- capture supervisor --------------------------------------------------------

    async def _supervise(self):
        loop = asyncio.get_running_loop()
        while True:
            if not self._capture_wanted():
                self._set_state("off")
                await self._changed.wait()
                self._changed.clear()
                continue
            try:
                if self._vad is None:
                    self._vad = await loop.run_in_executor(self._infer, self._vad_factory)
                if self.settings.get("enabled") and self._wake is None:
                    models = list(self.settings["wake_models"])
                    self._wake = await loop.run_in_executor(self._infer, self._wake_factory, models)
            except Exception as e:
                self._fail("load_models", e)
                continue

            self._restart_capture = False
            try:
                source = self._source_factory(self.settings)
                async with contextlib.aclosing(source):
                    exhausted = True
                    async for frame in source:
                        if self._restart_capture or not self._capture_wanted():
                            exhausted = False
                            break
                        if self.state == "off":
                            self._set_state("idle" if self.settings.get("enabled") else "off")
                        if self._manual_start_pending:
                            self._begin_session("manual")
                        await self._on_frame(frame)
                if exhausted:
                    self.source_done.set()
                    if self.in_session:
                        self._end_session("error")
                    self._failed = True
                    self._set_state("off")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # No mic, device unplugged, permission denied: voice goes to
                # "off" and the rest of the agent carries on regardless.
                self._fail("capture", e)

    # -- per-frame state machine ----------------------------------------------------

    def _wake_score(self, frame):  # inference thread
        if self._reset_wake:
            self._reset_wake = False
            self._wake.reset()
        return self._wake.score(frame)

    def _speech_prob(self, frame):  # inference thread
        if self._reset_vad:
            self._reset_vad = False
            self._vad.reset()
        return self._vad.speech_prob(frame)

    def _muted(self) -> bool:
        return self._speech_busy > 0 or time.monotonic() < self._mute_until

    def _reset_utterance(self) -> None:
        self._utterance = []
        self._preroll.clear()
        self._speech_frames = 0
        self._trailing_silence = 0

    async def _on_frame(self, frame: np.ndarray) -> None:
        # Half-duplex: while Leutheria is talking (and for a short echo tail
        # after), the mic hears Leutheria, so nothing it captures is used --
        # no wake detection, no VAD, no level meter.
        if self._muted():
            return

        state = self.state
        loop = asyncio.get_running_loop()

        if state == "idle":
            if not self.settings.get("enabled") or self._wake is None:
                return
            name, score = await loop.run_in_executor(self._infer, self._wake_score, frame)
            if score >= self.settings["wake_threshold"] and self.state == "idle":
                log_event("wake_detected", model=name, score=round(score, 3))
                self._begin_session("wake_word", name, round(score, 3))
            return

        if state in ("thinking", "speaking"):
            # Poll as well as be told: a confirmation can appear without being
            # spoken (TTS failed) and must still open the mic, and a missed
            # end-of-speech timer must not leave the session deaf. A pending
            # confirmation is only acted on here after ~1 s, because normally
            # its spoken prompt starts a moment after it's registered and the
            # speech hooks handle it -- acting at once would flicker the UI
            # thinking -> awaiting -> speaking -> awaiting.
            if self._confirmation_pending():
                self._pending_polls += 1
                if self._pending_polls < 12:
                    return
            self._pending_polls = 0
            self._settle()
            return

        if state == "awaiting_confirmation" and not self._confirmation_pending():
            # Answered by a click in the UI (or timed out) meanwhile.
            self._settle(force=True)
            state = self.state

        if state not in IN_SESSION_LISTENING:
            return  # transcribing / speaking: one utterance in flight at a time

        level = audio_level(frame)
        # Fast attack, slow release -- a meter that drops as fast as it rises
        # flickers between syllables.
        self._level += (0.6 if level > self._level else 0.25) * (level - self._level)
        self._emit({"type": "audio_level", "level": round(self._level, 3)})

        is_speech = await loop.run_in_executor(self._infer, self._speech_prob, frame) >= VAD_THRESHOLD
        if self.state != state:
            return  # something else moved the machine while VAD ran

        if state in ("listening", "awaiting_confirmation"):
            self._preroll.append(frame)
            if is_speech:
                self._utterance = list(self._preroll)
                self._speech_frames = 1
                self._trailing_silence = 0
                self._resume_state = state
                self._set_state("hearing")
            elif state == "listening":
                self._silence_frames += 1
                timeout_frames = self.settings["silence_timeout_s"] * 1000 / FRAME_MS
                if self._silence_frames >= timeout_frames:
                    self._end_session("timeout")
            return

        # hearing
        self._utterance.append(frame)
        if is_speech:
            self._speech_frames += 1
            self._trailing_silence = 0
        else:
            self._trailing_silence += 1
        endpoint_frames = math.ceil(self.settings["endpoint_silence_ms"] / FRAME_MS)
        too_long = len(self._utterance) * FRAME_MS >= MAX_UTTERANCE_MS
        if self._trailing_silence >= endpoint_frames or too_long:
            self._endpoint()

    def _endpoint(self) -> None:
        frames, speech_ms = self._utterance, self._speech_frames * FRAME_MS
        resume = self._resume_state
        self._reset_utterance()
        if speech_ms < MIN_SPEECH_MS:
            log_event("voice_utterance_rejected", reason="too_short", speech_ms=speech_ms)
            self._set_state(resume)
            return
        self._set_state("transcribing")
        self._spawn(self._transcribe(frames, resume, speech_ms))

    async def _transcribe(self, frames: list, resume: str, speech_ms: int) -> None:
        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        try:
            segments = await asyncio.get_running_loop().run_in_executor(None, self._transcriber, audio)
        except Exception as e:
            log_event("voice_error", stage="transcribe", error=str(e))
            segments = []

        if self.state != "transcribing":
            return  # session ended or speech started while Whisper ran

        kept = [s for s in segments if s.get("no_speech_prob", 0.0) <= NO_SPEECH_MAX]
        text = " ".join(s["text"].strip() for s in kept).strip()
        filler = bool(text) and all(w in _NON_WORDS for w in _words(text))
        dismissed = bool(text) and not filler and is_dismissal(text, self.settings["dismiss_phrases"])
        log_event(
            "voice_transcript",
            text=text,
            speech_ms=speech_ms,
            no_speech_probs=[round(s.get("no_speech_prob", 0.0), 3) for s in segments],
            dropped=[s["text"].strip() for s in segments if s not in kept],
            filler=filler,
            dismissed=dismissed,
        )
        if filler:
            text = ""

        if not text:
            self._set_state(resume)
            return

        if resume == "awaiting_confirmation" and self._confirmation_pending():
            if await self._on_confirmation_answer(text):
                self._set_state("thinking")
            elif dismissed:
                # "Thanks, that's all" over an open question is a no: decline
                # it now rather than leave it pending, unseen, until it times
                # out after the overlay has gone (BUGS.md #22).
                if self._on_dismiss_pending:
                    await self._on_dismiss_pending()
                self._end_session("dismissed")
            else:
                self._set_state("awaiting_confirmation")
            return

        if dismissed:
            self._end_session("dismissed")
            return

        if self._command_task is not None:
            # Single command in flight. Reaching here means the confirmation
            # this utterance was meant for was answered some other way first.
            log_event("voice_utterance_dropped", reason="command_in_flight", text=text)
            self._set_state("thinking")
            return

        self._silence_frames = 0
        self._set_state("thinking")
        self._command_task = self._spawn(self._run_command(text))

    async def _run_command(self, text: str) -> None:
        try:
            await self._on_command(text)
        except Exception as e:
            log_event("voice_error", stage="command", error=str(e))
        finally:
            self._command_task = None
            if not self._muted():
                self._settle()

    def _settle(self, force: bool = False) -> None:
        """Pick the resting state after speech or a command finishes: waiting
        on a yes/no if one is pending, still thinking if the command is still
        running, otherwise back to listening for the next request."""
        if not self.in_session:
            return
        if not force and self.state not in ("speaking", "thinking"):
            return
        if self._confirmation_pending():
            self._set_state("awaiting_confirmation")
        elif self._command_task is not None:
            self._set_state("thinking")
        else:
            if self.state != "listening":
                self._silence_frames = 0
                self._reset_vad = True
            self._set_state("listening")

    # -- tts observer (called from tts.speak on the event loop) ------------------------

    def speech_begin(self) -> None:
        self._speech_busy += 1
        if self.in_session and self.state in IN_SESSION_LISTENING:
            self._reset_utterance()  # whatever was being heard is now talked over

    def playback_started(self) -> None:
        if self.in_session:
            self._set_state("speaking")

    def speech_end(self) -> None:
        self._speech_busy = max(0, self._speech_busy - 1)
        self._mute_until = time.monotonic() + PLAYBACK_TAIL_S
        # A hair past the tail: firing exactly on it can land a float-rounding
        # instant early, see itself still muted, and never settle.
        asyncio.get_running_loop().call_later(PLAYBACK_TAIL_S + 0.02, self._after_speech)

    def _after_speech(self) -> None:
        if self._muted():
            return  # more speech queued behind this one; its own end settles
        self._settle()
