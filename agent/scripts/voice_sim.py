"""Drive the voice session state machine with synthesized speech instead of a
microphone, and check the messages it produces against the spec.

    cd agent
    ./.venv/bin/python scripts/voice_sim.py                 # all scenarios
    ./.venv/bin/python scripts/voice_sim.py dismiss timeout # just these
    ./.venv/bin/python scripts/voice_sim.py --list
    ./.venv/bin/python scripts/voice_sim.py --play          # hear the replies
    ./.venv/bin/python scripts/voice_sim.py --real-llm      # real agent loop

Each scenario is stitched from Piper utterances (the project's own voice) and
silence, resampled to 16 kHz, and fed frame by frame at real speed through
the same injectable source the mic uses. The session is wired to the real
server hooks -- server.run_voice_command, the shared transcript path, the
confirmation interception, tts.speak -- against a fake UI connection that
records everything sent to it. By default the agent loop is replaced with a
canned responder (no API key, no cost, deterministic) and playback is a
silent sleep of the clip's length, so nothing comes out of the speakers.

No microphone is ever opened. Scenarios run at real time, so the whole
suite takes a couple of minutes.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from agent.core import agent_loop, audio_io, server, stt, tts, voice_session  # noqa: E402
from agent.core.dispatcher import dispatch  # noqa: E402

RATE = audio_io.SAMPLE_RATE
_clip_cache: dict = {}


def say(text: str) -> np.ndarray:
    if text not in _clip_cache:
        audio, rate = audio_io.wav_bytes_to_array(tts.synthesize(text))
        _clip_cache[text] = audio_io.resample(audio, rate, RATE)
    return _clip_cache[text]


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), dtype=np.int16)


def noise(seconds: float, level: float = 3000) -> np.ndarray:
    rng = np.random.default_rng(0)
    return np.clip(rng.normal(0, level, int(seconds * RATE)), -32768, 32767).astype(np.int16)


def blip(seconds: float) -> np.ndarray:
    """The first `seconds` of actual voice in a spoken word -- real speech,
    but shorter than the 300 ms the session requires before asking Whisper."""
    word = say("Stop.")
    onset = int(np.argmax(np.abs(word) > 1000))
    return word[onset : onset + int(seconds * RATE)]


class FakeUI:
    """Stands in for the Electron connection: records every message."""

    def __init__(self, t0: float):
        self.t0 = t0
        self.messages: list = []

    async def send(self, raw: str) -> None:
        self.messages.append((time.monotonic() - self.t0, json.loads(raw)))


async def canned_agent(backend, user_text: str, websocket=None, pending=None) -> dict:
    """A deterministic stand-in for agent_loop.run. "echo" requests go through
    the real dispatcher so the destructive-tool confirmation (and answering
    it by voice) is exercised for real."""
    if "echo" in user_text.lower() or "shell" in user_text.lower():
        result = await dispatch(
            "run_command",
            {"cmd": "echo hello"},
            websocket,
            pending,
            "I'd like to run echo hello. Should I go ahead?",
        )
        text = f"Done. It printed {result.get('stdout')}." if result.get("ok") else "Okay, I won't run it."
        return {"type": "response", "text": text, "trace": [{"tool": "run_command", "result": result}]}
    return {"type": "response", "text": "It's three o'clock.", "trace": []}


def silent_player(audio, rate) -> None:
    time.sleep(len(audio) / rate)


# --- scenarios -----------------------------------------------------------------
# parts: np arrays, or ("start",)/("end",) manual control actions at that point.
# expect: the exact sequence of voice_state states after the initial ones,
# consecutive duplicates collapsed.


def scenarios() -> dict:
    return {
        "dismiss": {
            "about": "wake word, one command, then 'thanks' ends the session",
            "parts": [silence(1), say("Hey Jarvis."), silence(1), say("What time is it?"), silence(5),
                      say("Thanks."), silence(2)],
            "expect_states": ["idle", "listening", "hearing", "transcribing", "thinking", "speaking",
                              "listening", "hearing", "transcribing", "idle"],
            "expect_ended": ["dismissed"],
            "expect_transcripts": 1,
        },
        "long_thanks": {
            "about": "a long sentence containing 'thanks' is a command, not a dismissal",
            "parts": [silence(1), say("Hey Jarvis."), silence(1),
                      say("Thanks, can you open my calendar and check what is on for Thursday afternoon?"),
                      silence(9)],
            "settings": {"silence_timeout_s": 3},
            "expect_states": ["idle", "listening", "hearing", "transcribing", "thinking", "speaking",
                              "listening", "idle"],
            "expect_ended": ["timeout"],
            "expect_transcripts": 1,
        },
        "timeout": {
            "about": "wake word then nothing: the silence timeout ends the session",
            "parts": [silence(1), say("Hey Jarvis."), silence(5)],
            "settings": {"silence_timeout_s": 3},
            "expect_states": ["idle", "listening", "idle"],
            "expect_ended": ["timeout"],
            "expect_transcripts": 0,
        },
        "noise": {
            "about": "white noise, a 200 ms speech blip, 'hm' and silence never become a command",
            "parts": [silence(1), say("Hey Jarvis."), silence(1), noise(3), silence(1), blip(0.2),
                      silence(1.5), say("Hm."), silence(1.5), noise(2, level=8000), silence(4)],
            "settings": {"silence_timeout_s": 8},
            # noise/blips may briefly enter "hearing", and "Hm." is real speech
            # that reaches Whisper -- what must never happen is a command.
            "expect_states": None,
            "expect_ended": ["timeout"],
            "expect_transcripts": 0,
        },
        "confirm": {
            "about": "destructive step: spoken prompt, unclear reply ignored, 'yes' approves, then dismissal",
            "parts": [silence(1), say("Hey Jarvis."), silence(1), say("Run the shell command echo hello."),
                      silence(6), say("What does that do?"), silence(3), say("Yes, go ahead."), silence(6),
                      say("That's all, thanks."), silence(2)],
            "expect_states": ["idle", "listening", "hearing", "transcribing", "thinking", "speaking",
                              "awaiting_confirmation", "hearing", "transcribing", "awaiting_confirmation",
                              "hearing", "transcribing", "thinking", "speaking", "listening", "hearing",
                              "transcribing", "idle"],
            "expect_ended": ["dismissed"],
            "expect_transcripts": 3,
        },
        "dismiss_pending": {
            "about": "'thanks' over an open confirmation declines it and ends the session (BUGS.md #22)",
            "parts": [silence(1), say("Hey Jarvis."), silence(1), say("Run the shell command echo hello."),
                      silence(6), say("Thanks."), silence(4)],
            "expect_states": None,
            "expect_prefix": ["idle", "listening", "hearing", "transcribing", "thinking", "speaking",
                              "awaiting_confirmation", "hearing", "transcribing", "idle"],
            "expect_ended": ["dismissed"],
            "expect_transcripts": 2,
            "expect_resolved": [False],
        },
        "manual": {
            "about": "manual start (no wake word), one command, manual end",
            "parts": [silence(0.5), ("start",), silence(0.5), say("What time is it?"), silence(5), ("end",),
                      silence(1)],
            "expect_states": ["idle", "listening", "hearing", "transcribing", "thinking", "speaking",
                              "listening", "idle"],
            "expect_ended": ["manual"],
            "expect_transcripts": 1,
        },
    }


async def run_scenario(name: str, spec: dict, real_llm: bool) -> bool:
    settings = {**voice_session_defaults(), **spec.get("settings", {})}
    t0 = time.monotonic()
    ui = FakeUI(t0)
    pending: dict = {}
    server._connections.append((ui, pending, {}))

    actions = []  # (frame index, action)
    audio_parts = []
    frame_pos = 0
    for part in spec["parts"]:
        if isinstance(part, tuple):
            actions.append((frame_pos, part[0]))
        else:
            audio_parts.append(part)
            frame_pos += len(part) / audio_io.FRAME_SAMPLES
    audio = np.concatenate(audio_parts)
    clip_done = asyncio.Event()
    holder = {}

    async def source(_settings):
        index = 0
        async for frame in audio_io.array_frames(audio, realtime=1.0):
            for at, action in actions:
                if int(at) == index:
                    holder["session"].request(action)
            index += 1
            yield frame
        clip_done.set()
        await asyncio.Event().wait()  # a real mic never ends; hold here until closed

    session = voice_session.VoiceSession(
        settings,
        emit=server.broadcast,
        on_command=server.run_voice_command,
        on_confirmation_answer=server.answer_confirmation_by_voice,
        confirmation_pending=server.confirmation_pending,
        on_dismiss_pending=server.decline_pending_by_dismissal,
        source_factory=source,
    )
    holder["session"] = session
    session.start()
    await clip_done.wait()
    await asyncio.sleep(0.5)
    await session.close()
    server._connections.clear()

    return report(name, spec, ui.messages)


def voice_session_defaults() -> dict:
    from agent.core.voice_settings import DEFAULTS

    return dict(DEFAULTS)


def report(name: str, spec: dict, messages: list) -> bool:
    print(f"\n=== {name}: {spec['about']}")
    levels = []
    for t, m in messages:
        if m["type"] == "audio_level":
            levels.append(m["level"])
            continue
        if levels:
            print(f"          ({len(levels)} audio_level msgs, max {max(levels):.2f})")
            levels = []
        body = {k: v for k, v in m.items() if k not in ("type", "trace")}
        print(f"  {t:6.2f}s {m['type']:<22} {json.dumps(body)}")

    states = []
    for _, m in messages:
        if m["type"] == "voice_state" and (not states or states[-1] != m["state"]):
            states.append(m["state"])
    ended = [m["reason"] for _, m in messages if m["type"] == "session_ended"]
    transcripts = [m for _, m in messages if m["type"] == "transcript"]
    level_states_ok = check_levels(messages)

    problems = []
    if spec["expect_states"] is not None and states != spec["expect_states"]:
        problems.append(f"states {states} != expected {spec['expect_states']}")
    prefix = spec.get("expect_prefix")
    if prefix is not None and states[: len(prefix)] != prefix:
        problems.append(f"states {states} don't start with {prefix}")
    resolved = [m["approved"] for _, m in messages if m["type"] == "confirmation_resolved"]
    if "expect_resolved" in spec and resolved != spec["expect_resolved"]:
        problems.append(f"confirmation_resolved {resolved} != {spec['expect_resolved']}")
    if spec["expect_states"] is None and prefix is None and "thinking" in states:
        problems.append(f"became a command: {states}")
    if ended != spec["expect_ended"]:
        problems.append(f"session_ended {ended} != {spec['expect_ended']}")
    if len(transcripts) != spec["expect_transcripts"]:
        problems.append(f"{len(transcripts)} transcripts, expected {spec['expect_transcripts']}")
    if any(t.get("origin") != "voice_session" for t in transcripts):
        problems.append("transcript without origin=voice_session")
    if not level_states_ok:
        problems.append("audio_level sent outside listening/hearing/awaiting_confirmation")
    print(f"  states: {' -> '.join(states)}")
    print(f"  {'PASS' if not problems else 'FAIL: ' + '; '.join(problems)}")
    return not problems


def check_levels(messages: list) -> bool:
    state, in_session = "off", False
    for _, m in messages:
        if m["type"] == "voice_state":
            state, in_session = m["state"], m["session"]
        elif m["type"] == "audio_level":
            if not in_session or state not in voice_session.IN_SESSION_LISTENING:
                return False
    return True


def unit_checks() -> bool:
    print("\n=== dismissal matcher")
    phrases = voice_session_defaults()["dismiss_phrases"]
    cases = {
        "Thanks.": True, "Thank you.": True, "That's all, thanks.": True, "Okay, that's it.": True,
        "Goodbye!": True, "Thank you so much.": True, "No, that's all for now.": True,
        "Thanks, what's the weather?": False, "Thanks for opening my calendar and the notes app.": False,
        "What time is it?": False, "That's it, now open Safari.": False, "Thanksgiving": False,
    }
    ok = True
    for text, expected in cases.items():
        got = voice_session.is_dismissal(text, phrases)
        ok &= got == expected
        print(f"  {'ok ' if got == expected else 'BAD'} {text!r:55} -> {got}")

    print("\n=== Whisper on non-speech (what the guard is for), no_speech_prob per segment")
    for label, clip in [("2 s silence", silence(2)), ("2 s white noise", noise(2))]:
        segments = stt.transcribe_segments(clip.astype(np.float32) / 32768.0)
        described = [(s["text"].strip(), round(s["no_speech_prob"], 2)) for s in segments]
        kept = [s for s in segments if s["no_speech_prob"] <= voice_session.NO_SPEECH_MAX]
        print(f"  {label:16} segments={described} kept={[s['text'].strip() for s in kept]}")
    return ok


async def failure_checks() -> bool:
    """Voice must fail to "off" -- logged, never raised -- and recover."""
    print("\n=== failure handling")
    ok = True

    def expect(cond, what):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'ok ' if cond else 'BAD'} {what}")

    ui = FakeUI(time.monotonic())
    server._connections.append((ui, {}, {}))

    # 1. an unknown wake model fails to load -> off; fixing the setting recovers
    session = voice_session.VoiceSession(
        {**voice_session_defaults(), "wake_models": ["not_a_real_model"]},
        emit=server.broadcast, on_command=server.run_voice_command,
        on_confirmation_answer=server.answer_confirmation_by_voice,
        confirmation_pending=server.confirmation_pending,
        on_dismiss_pending=server.decline_pending_by_dismissal,
        source_factory=lambda s: audio_io.array_frames(silence(60), realtime=1.0),
    )
    session.start()
    await asyncio.sleep(2)
    expect(session.state == "off", f"bad wake model -> state {session.state}")
    session.apply_settings(voice_session_defaults())
    await asyncio.sleep(3)
    expect(session.state == "idle", f"fixed wake model -> state {session.state}")
    await session.close()

    # 2. the device dies mid-session -> session_ended error, off
    ui.messages.clear()

    async def dying_source(_settings):
        async for i, frame in _enumerate(audio_io.array_frames(silence(10), realtime=1.0)):
            if i == 10:
                raise OSError("PortAudio: device unavailable (simulated)")
            yield frame

    session = voice_session.VoiceSession(
        voice_session_defaults(), emit=server.broadcast, on_command=server.run_voice_command,
        on_confirmation_answer=server.answer_confirmation_by_voice,
        confirmation_pending=server.confirmation_pending,
        on_dismiss_pending=server.decline_pending_by_dismissal, source_factory=dying_source,
    )
    session.start()
    await asyncio.sleep(0.3)
    session.request("start")
    await asyncio.sleep(2)
    ended = [m["reason"] for _, m in ui.messages if m["type"] == "session_ended"]
    expect(ended == ["error"] and session.state == "off", f"device lost -> ended {ended}, state {session.state}")
    await session.close()
    server._connections.clear()
    return ok


async def _enumerate(aiter):
    i = 0
    async for item in aiter:
        yield i, item
        i += 1


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--play", action="store_true", help="play replies through the speakers")
    parser.add_argument("--real-llm", action="store_true", help="use the real agent loop (needs API key)")
    args = parser.parse_args()

    all_scenarios = scenarios()
    if args.list:
        for name, spec in all_scenarios.items():
            print(f"{name:12} {spec['about']}")
        return
    if not args.play:
        tts.set_player(silent_player)
    if args.real_llm:
        from dotenv import load_dotenv

        load_dotenv()
    else:
        agent_loop.run = canned_agent
        server.get_backend = lambda: None

    results = {"unit": unit_checks(), "failures": await failure_checks()} if not args.names else {}
    for name in args.names or all_scenarios:
        results[name] = await run_scenario(name, all_scenarios[name], args.real_llm)

    print("\n=== summary")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    asyncio.run(main())
