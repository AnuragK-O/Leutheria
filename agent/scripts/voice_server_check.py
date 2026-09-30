"""End-to-end check of the voice session through the real server process.

    cd agent
    ./.venv/bin/python scripts/voice_server_check.py [--port 8781]

Starts `python -m agent` on a side port (never 8765, so a running app is
left alone) with LEUTHERIA_VOICE_SOURCE pointing at a synthesized WAV --
"hey jarvis", a question, "thanks" -- so the mic is never opened. Then,
over a websocket like the Electron app's:

  1. get_voice_settings / set_voice_settings (a bad value is rejected,
     a good one is applied and written to voice_settings.json)
  2. the WAV plays: wake word -> real agent loop (needs ANTHROPIC_API_KEY)
     -> reply spoken through the real speakers -> "thanks" dismisses
  3. disabling voice turns it off; a manual start/end still works while off
  4. restarts the server and checks the settings survived

voice_settings.json is backed up first and restored at the end.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

import numpy as np  # noqa: E402
import websockets  # noqa: E402

from agent.core import audio_io, tts, voice_settings  # noqa: E402

RATE = audio_io.SAMPLE_RATE
WAV_PATH = Path(os.environ.get("TMPDIR", "/tmp")) / "leutheria_voice_check.wav"


def build_wav() -> None:
    def say(text):
        audio, rate = audio_io.wav_bytes_to_array(tts.synthesize(text))
        return audio_io.resample(audio, rate, RATE)

    def gap(seconds):
        return np.zeros(int(seconds * RATE), dtype=np.int16)

    # Leading silence covers model loading; the long gap covers a real LLM
    # round trip plus the spoken reply before "thanks".
    audio = np.concatenate([gap(4), say("Hey Jarvis."), gap(1), say("What is two plus two?"), gap(14),
                            say("Thanks."), gap(1)])
    with wave.open(str(WAV_PATH), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(audio.tobytes())


def ptt_clip() -> str:
    import base64

    return base64.b64encode(tts.synthesize("What is three plus three?")).decode()


def start_server(port: int) -> subprocess.Popen:
    env = {**os.environ, "LEUTHERIA_PORT": str(port), "LEUTHERIA_VOICE_SOURCE": str(WAV_PATH)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent"], cwd=AGENT_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    line = proc.stdout.readline()
    if "AGENT_READY" not in line:
        proc.kill()
        raise SystemExit(f"server did not start: {line!r}")
    return proc


class Client:
    def __init__(self, ws, t0):
        self.ws, self.t0 = ws, t0
        self.log: list = []
        self._results: dict = {}
        self._next_id = 0
        self._reader = asyncio.create_task(self._read())

    async def _read(self):
        async for raw in self.ws:
            message = json.loads(raw)
            if message["type"] == "control_result":
                self._results[message["request_id"]].set_result(message)
                continue
            self.log.append((time.monotonic() - self.t0, message))
            if message["type"] != "audio_level":
                body = {k: v for k, v in message.items() if k not in ("type", "trace")}
                print(f"  {time.monotonic() - self.t0:6.2f}s <- {message['type']:<16} {json.dumps(body)[:110]}")

    async def control(self, type_, **fields):
        self._next_id += 1
        request_id = str(self._next_id)
        self._results[request_id] = asyncio.get_running_loop().create_future()
        await self.ws.send(json.dumps({"type": type_, "request_id": request_id, **fields}))
        result = await asyncio.wait_for(self._results[request_id], 10)
        print(f"  {time.monotonic() - self.t0:6.2f}s ctl {type_} {json.dumps(fields)} -> "
              f"{json.dumps({k: v for k, v in result.items() if k not in ('type', 'request_id')})}")
        return result

    async def wait_for(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(predicate(m) for _, m in self.log):
                return True
            await asyncio.sleep(0.1)
        return False


async def run(port: int) -> list:
    failures = []

    def check(ok, what):
        print(f"  [{'ok' if ok else 'FAIL'}] {what}")
        if not ok:
            failures.append(what)

    print("\n--- run 1: settings, a live session, disable, manual start/end")
    proc = start_server(port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            c = Client(ws, time.monotonic())
            r = await c.control("get_voice_settings")
            check(r["ok"] and r["settings"] == voice_settings.DEFAULTS, "get_voice_settings returns defaults")
            r = await c.control("set_voice_settings", settings={"wake_threshold": 2})
            check(not r["ok"], "out-of-range wake_threshold rejected")
            r = await c.control("set_voice_settings", settings={"bogus": 1})
            check(not r["ok"], "unknown key rejected")
            r = await c.control("set_voice_settings", settings={"silence_timeout_s": 60})
            check(r["ok"] and r["settings"]["silence_timeout_s"] == 60, "silence_timeout_s set")
            on_disk = json.loads(voice_settings.SETTINGS_FILE.read_text())
            check(on_disk["silence_timeout_s"] == 60, "persisted to voice_settings.json")

            ended = await c.wait_for(lambda m: m["type"] == "session_ended", 60)
            check(ended, "session ran and ended")
            types = [m["type"] for _, m in c.log]
            check("speech" not in types, "no legacy `speech` message")
            started = [m for _, m in c.log if m["type"] == "session_started"]
            check(bool(started) and started[0]["trigger"] == "wake_word", "session_started by wake word")
            transcripts = [m for _, m in c.log if m["type"] == "transcript"]
            check(bool(transcripts) and transcripts[0].get("origin") == "voice_session", "transcript origin")
            responses = [m for _, m in c.log if m["type"] == "response"]
            check(bool(responses) and responses[0].get("origin") == "voice_session", "response origin")
            speaking = [m for _, m in c.log if m["type"] == "speaking"]
            check([m["state"] for m in speaking[:2]] == ["started", "ended"], "speaking started/ended")
            reasons = [m["reason"] for _, m in c.log if m["type"] == "session_ended"]
            check(reasons == ["dismissed"], f"ended by dismissal ({reasons})")

            c.log.clear()
            r = await c.control("set_voice_settings", settings={"enabled": False})
            await c.wait_for(lambda m: m["type"] == "voice_state" and m["state"] == "off", 5)
            check(any(m.get("state") == "off" for _, m in c.log), "disabling -> voice_state off")

            # Push-to-talk shares the transcript path; make sure the refactor
            # kept it working (and tagged) with voice off.
            c.log.clear()
            await ws.send(json.dumps({"type": "audio", "format": "wav", "data": ptt_clip()}))
            got = await c.wait_for(lambda m: m["type"] == "speaking" and m["state"] == "ended", 60)
            transcripts = [m for _, m in c.log if m["type"] == "transcript"]
            check(bool(transcripts) and transcripts[0].get("origin") == "push_to_talk", "push-to-talk transcript")
            responses = [m for _, m in c.log if m["type"] == "response"]
            check(bool(responses) and responses[0]["text"] and "origin" not in responses[0],
                  "push-to-talk response (untagged)")
            check(got, "push-to-talk reply spoken")

            c.log.clear()
            r = await c.control("voice_session", action="start")
            check(r["ok"], "manual start accepted while disabled")
            got = await c.wait_for(lambda m: m["type"] == "session_started" and m["trigger"] == "manual", 10)
            check(got, "session_started trigger=manual")
            r = await c.control("voice_session", action="end")
            got = await c.wait_for(lambda m: m["type"] == "session_ended" and m["reason"] == "manual", 5)
            check(got, "session_ended reason=manual")
            got = await c.wait_for(lambda m: m["type"] == "voice_state" and m["state"] == "off", 5)
            check(got, "back to off after a manual session while disabled")
            r = await c.control("voice_session", action="dance")
            check(not r["ok"], "unknown action rejected")
            c._reader.cancel()
    finally:
        proc.kill()
        proc.wait()

    print("\n--- run 2: settings survive a restart")
    proc = start_server(port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            c = Client(ws, time.monotonic())
            r = await c.control("get_voice_settings")
            check(r["settings"]["enabled"] is False and r["settings"]["silence_timeout_s"] == 60,
                  "settings persisted across restart")
            await asyncio.sleep(1)
            states = [m["state"] for _, m in c.log if m["type"] == "voice_state"]
            check(states[:1] == ["off"], f"disabled voice starts off ({states})")
            c._reader.cancel()
    finally:
        proc.kill()
        proc.wait()
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8781)
    args = parser.parse_args()
    if args.port == 8765:
        raise SystemExit("use a side port -- 8765 belongs to the running app")

    build_wav()
    backup = voice_settings.SETTINGS_FILE.read_text() if voice_settings.SETTINGS_FILE.exists() else None
    voice_settings.SETTINGS_FILE.unlink(missing_ok=True)
    try:
        failures = asyncio.run(run(args.port))
    finally:
        if backup is None:
            voice_settings.SETTINGS_FILE.unlink(missing_ok=True)
        else:
            voice_settings.SETTINGS_FILE.write_text(backup)
        WAV_PATH.unlink(missing_ok=True)
    print("\nPASS" if not failures else f"\nFAIL: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
