"""Check the swappable TTS backends without a real ElevenLabs key.

    cd agent
    ./.venv/bin/python scripts/tts_check.py

A fake ElevenLabs server (http.server on a spare localhost port) stands in
for api.elevenlabs.io. It records every request and answers according to the
scenario: PCM audio, a 401/500 with the API's JSON error shape, a 200 that is
JSON instead of audio, or a response slower than the client's read timeout.
A fake subprocess.run stands in for macOS `say` in every reply-path test.

Checked:
  - the request shape: POST /v1/text-to-speech/{voice_id}?output_format=pcm_24000,
    xi-api-key header, JSON body {text, model_id}
  - decoding: little-endian int16 PCM -> the exact samples, at 24000 Hz
  - the real speak() path with tts_backend=elevenlabs: the fake player gets
    the ElevenLabs samples, `speaking` says backend=elevenlabs
  - fallbacks -- no key, 401, 500, JSON-not-audio, timeout, connection
    refused, an unknown backend, a bad `say` voice, a bad Piper voice, Piper
    not installed: each is spoken by the macOS `say` fallback and logs
    tts_error naming the backend that failed; `say` itself failing leaves the
    reply silent with a tts_error (and no `speaking` message)
  - the `say` backend with a stubbed subprocess: the fixed argv (text on
    stdin, never in argv), the temp file read back and removed, voice names
    validated against a cached `say -v '?'` listing, timeout / non-zero exit /
    no file / empty audio as TTSError -- plus ONE real `say -o` render of a
    short word to a temp file (writes a file, plays nothing)
  - Piper absent: with the `piper` import blocked, a fresh interpreter
    imports tts and control without error and reports piper unavailable
  - settings validation: tts_backend / tts_voice accepted and rejected as
    they should be, and nothing key-shaped is ever written to the file
  - availability() is a boolean that follows the env var

Nothing is played through the speakers (the player is a list, and `say` only
ever runs with -o), events.jsonl is not written to (log_event is captured),
and voice_settings.json is never touched (a temp file stands in for it).
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

import numpy as np  # noqa: E402

from agent.core import tts, tts_backends, voice_settings  # noqa: E402

FAKE_KEY = "test-key-not-real-123"
# A recognisable waveform: 0.25 s of a 440 Hz tone at 24 kHz.
PCM_SAMPLES = (np.sin(np.arange(6000) * 2 * np.pi * 440 / 24000) * 12000).astype("<i2")
# What the fake `say` "renders": 0.1 s of a 300 Hz tone at 22050 Hz.
SAY_RATE = 22050
SAY_SAMPLES = (np.sin(np.arange(2205) * 2 * np.pi * 300 / SAY_RATE) * 9000).astype("<i2")
SAY_LISTING = (
    "Albert              en_US    # Hello! My name is Albert.\n"
    "Samantha            en_US    # Hello! My name is Samantha.\n"
    "Eddy (English (US)) en_US    # Hello! My name is Eddy.\n"
    "Amélie              fr_CA    # Bonjour! Je m'appelle Amélie.\n"
    "not a voice line\n"
)


class FakeElevenLabs(BaseHTTPRequestHandler):
    mode = "ok"
    requests = []

    def log_message(self, *args):  # keep the output readable
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        url = urlparse(self.path)
        FakeElevenLabs.requests.append(
            {
                "path": url.path,
                "query": parse_qs(url.query),
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(body or b"{}"),
            }
        )
        mode = FakeElevenLabs.mode
        if mode == "slow":
            time.sleep(3)
        if mode in ("401", "500"):
            payload = json.dumps({"detail": {"status": "invalid_api_key" if mode == "401" else "error",
                                             "message": "Invalid API key" if mode == "401" else "internal"}})
            self._reply(int(mode), "application/json", payload.encode())
        elif mode == "json200":
            self._reply(200, "application/json", b'{"detail": "not audio"}')
        else:
            self._reply(200, "audio/pcm", PCM_SAMPLES.tobytes())

    def _reply(self, status, content_type, data):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client already gave up (the timeout scenario)


class FakeSay:
    """Stands in for subprocess.run inside MacSayBackend: answers the voice
    listing with SAY_LISTING and, for a render, writes a WAV to the -o path
    the way `say` would. Runs nothing."""

    def __init__(self):
        self.mode = "ok"
        self.list_ok = True
        self.calls = []
        self.outputs = []

    def renders(self):
        return [c for c in self.calls if "-o" in c["argv"]]

    def listings(self):
        return [c for c in self.calls if c["argv"][1:] == ["-v", "?"]]

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        if argv[1:] == ["-v", "?"]:
            if not self.list_ok:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, SAY_LISTING, "")
        if self.mode == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        if self.mode == "exit":
            return subprocess.CompletedProcess(argv, 1, "", "Opening output file failed")
        out = Path(argv[argv.index("-o") + 1])
        self.outputs.append(out)
        if self.mode == "nofile":
            return subprocess.CompletedProcess(argv, 0, "", "")
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAY_RATE)
            wav.writeframes(b"" if self.mode == "empty" else SAY_SAMPLES.tobytes())
        return subprocess.CompletedProcess(argv, 0, "", "")


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


failures = []
logged = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'' if ok else '  -- ' + str(detail)}")
    if not ok:
        failures.append(label)


def run_speak(settings):
    """speak() once with the given voice settings; returns (played, sent, logged)."""
    played = []
    tts.set_player(lambda audio, rate: played.append((np.asarray(audio), rate)))
    voice_settings.load = lambda: {**voice_settings.DEFAULTS, **settings}
    logged.clear()
    socket = FakeSocket()
    asyncio.run(tts.speak(socket, "Testing the voice."))
    return played, socket.sent, list(logged)


def said_by_say(played, sent):
    started = [m for m in sent if m.get("state") == "started"]
    return (
        len(played) == 1
        and played[0][1] == SAY_RATE
        and np.array_equal(played[0][0], SAY_SAMPLES)
        and bool(started) and started[0].get("backend") == "say"
    )


def raises(fn, fragment):
    try:
        fn()
    except tts_backends.TTSError as e:
        return fragment in str(e), str(e)
    except Exception as e:  # anything but TTSError is a failure here
        return False, f"{type(e).__name__}: {e}"
    return False, "no error"


def check_say_backend(fake_say):
    print("\n`say` backend (stubbed subprocess)")
    backend = tts_backends.MacSayBackend(run=fake_say)
    audio, rate = backend.synthesize("-v Albert hello there")
    render = fake_say.renders()[-1]
    argv = render["argv"]
    out = fake_say.outputs[-1]
    check("argv is fixed: say -o <tmp> WAVE LEI16@22050 -f -",
          argv[0] == "/usr/bin/say" and argv[1] == "-o"
          and argv[3:] == ["--file-format=WAVE", "--data-format=LEI16@22050", "-f", "-"], argv)
    check("text goes on stdin, never in argv",
          render.get("input") == "-v Albert hello there" and not any("hello" in a for a in argv), render)
    check("render has a timeout", render.get("timeout") == backend.TIMEOUT_S, render.get("timeout"))
    check("no voice -> no -v (system voice), no listing needed", "-v" not in argv and not fake_say.listings(), argv)
    check("output in a private leutheria-tts- temp dir, removed afterwards",
          out.parent.name.startswith("leutheria-tts-") and not out.parent.exists(), out)
    check("samples and rate read back exactly", rate == SAY_RATE and np.array_equal(audio, SAY_SAMPLES), (rate, len(audio)))

    for name in ("Eddy (English (US))", "Amélie", "Samantha"):
        backend.synthesize("Hi.", name)
        argv = fake_say.renders()[-1]["argv"]
        check(f"listed voice {name!r} passed as -v", argv[1:3] == ["-v", name], argv)
    check("voice listing cached: `say -v ?` ran once", len(fake_say.listings()) == 1, len(fake_say.listings()))
    check("listing parse skips non-voice lines", "not a voice line" not in backend.voices()
          and len(backend.voices()) == 4, sorted(backend.voices()))

    before = len(fake_say.renders())
    for voice, fragment in [("Zarvox2", "not installed"), ("../etc", "invalid"), ("-o", "invalid"),
                            ("Albert\n", "invalid"), ("a/b", "invalid"), ("", "")]:
        if voice == "":
            continue  # empty means the default voice, covered above
        ok, detail = raises(lambda: backend.synthesize("Hi.", voice), fragment)
        check(f"voice {voice!r} refused ({fragment})", ok, detail)
    check("refused voices never reach a render", len(fake_say.renders()) == before)

    for mode, fragment in [("timeout", "timed out"), ("exit", "exit 1"), ("nofile", "no audio file"),
                           ("empty", "empty audio")]:
        fake_say.mode = mode
        ok, detail = raises(lambda: backend.synthesize("Hi."), fragment)
        leftovers = [o for o in fake_say.outputs if o.parent.exists()]
        check(f"{mode}: TTSError ({fragment}), temp dir removed", ok and not leftovers, (detail, leftovers))
    fake_say.mode = "ok"

    flaky = FakeSay()
    flaky.list_ok = False
    again = tts_backends.MacSayBackend(run=flaky)
    ok, detail = raises(lambda: again.synthesize("Hi.", "Albert"), "failed")
    flaky.list_ok = True
    again.synthesize("Hi.", "Albert")
    check("a failed listing is a TTSError and isn't cached (retry works)",
          ok and len(flaky.listings()) == 2 and flaky.renders(), detail)


def check_piper_absent_fresh_interpreter():
    code = (
        "import sys, json; sys.modules['piper'] = None\n"
        "from agent.core import tts, control, tts_backends\n"
        "b = tts_backends.PiperBackend()\n"
        "try:\n    b._load('en_US-amy-medium'); err = ''\n"
        "except tts_backends.TTSError as e:\n    err = str(e)\n"
        "print(json.dumps({'avail': tts.availability(), 'err': err}))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=AGENT_DIR, capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = None
    check("piper blocked: fresh import of tts + control succeeds, piper unavailable, load says not installed",
          result.returncode == 0 and out and out["avail"].get("piper") is False and out["avail"].get("say") is True
          and "not installed" in out["err"], (result.returncode, result.stdout[-300:], result.stderr[-500:]))


def main():
    real_load = voice_settings.load
    tts.log_event = lambda event, **fields: logged.append({"event": event, **fields})
    saved_key = os.environ.pop("ELEVENLABS_API_KEY", None)
    real_backends = dict(tts_backends.BACKENDS)

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeElevenLabs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    fake = tts_backends.ElevenLabsBackend(base_url=base, timeout=(2, 1))
    tts_backends.BACKENDS["elevenlabs"] = fake
    fake_say = FakeSay()
    tts_backends.BACKENDS["say"] = tts_backends.MacSayBackend(run=fake_say)
    piper_installed = tts_backends.BACKENDS["piper"].available()
    print(f"fake ElevenLabs at {base}; fake `say`; Piper {'installed' if piper_installed else 'not installed'}\n")

    try:
        print("availability")
        check("no key -> elevenlabs unavailable, say available",
              tts.availability() == {"say": True, "piper": piper_installed, "elevenlabs": False}, tts.availability())
        os.environ["ELEVENLABS_API_KEY"] = FAKE_KEY
        avail = tts.availability()
        check("key set -> elevenlabs available, booleans only",
              avail == {"say": True, "piper": piper_installed, "elevenlabs": True}
              and FAKE_KEY not in json.dumps(avail), avail)

        print("\nrequest shape + decoding (backend directly)")
        FakeElevenLabs.mode, FakeElevenLabs.requests = "ok", []
        audio, rate = fake.synthesize("Hello there.", "abcDEF123_-x")
        req = FakeElevenLabs.requests[-1]
        check("path is /v1/text-to-speech/{voice_id}", req["path"] == "/v1/text-to-speech/abcDEF123_-x", req["path"])
        check("output_format=pcm_24000", req["query"] == {"output_format": ["pcm_24000"]}, req["query"])
        check("xi-api-key header carries the key", req["headers"].get("xi-api-key") == FAKE_KEY)
        check("JSON content type", req["headers"].get("content-type") == "application/json",
              req["headers"].get("content-type"))
        check("body is {text, model_id}", req["body"] == {"text": "Hello there.", "model_id": "eleven_flash_v2_5"},
              req["body"])
        check("rate is 24000", rate == 24000, rate)
        check("samples decode exactly (int16 LE)",
              audio.dtype == np.int16 and np.array_equal(audio, PCM_SAMPLES), (audio.dtype, len(audio)))
        fake.synthesize("Default voice.")
        check("null voice -> default voice id",
              FakeElevenLabs.requests[-1]["path"] == f"/v1/text-to-speech/{fake.DEFAULT_VOICE}",
              FakeElevenLabs.requests[-1]["path"])
        ok, detail = raises(lambda: fake.synthesize("Hi.", "Eddy (English (US))"), "invalid voice id")
        check("a macOS-style voice name is refused before any request", ok, detail)

        print("\nspeak() with tts_backend=elevenlabs")
        played, sent, events = run_speak({"tts_backend": "elevenlabs", "tts_voice": "myVoice42"})
        check("player got the ElevenLabs samples at 24 kHz",
              len(played) == 1 and played[0][1] == 24000 and np.array_equal(played[0][0], PCM_SAMPLES),
              [(len(a), r) for a, r in played])
        started = [m for m in sent if m.get("state") == "started"]
        check("speaking started says backend=elevenlabs, duration 250 ms",
              started and started[0].get("backend") == "elevenlabs" and started[0]["duration_ms"] == 250, started)
        check("speaking ended sent", sent and sent[-1] == {"type": "speaking", "state": "ended"}, sent)
        check("configured voice id used", FakeElevenLabs.requests[-1]["path"].endswith("/myVoice42"))
        check("no tts_error", not events, events)

        print("\nfallbacks to `say`")
        scenarios = [
            ("no key", None, "no API key"),
            ("HTTP 401", "401", "HTTP 401: Invalid API key"),
            ("HTTP 500", "500", "HTTP 500"),
            ("200 with JSON, not audio", "json200", "expected audio"),
            ("read timeout", "slow", "timed out"),
        ]
        for label, mode, reason in scenarios:
            if mode is None:
                os.environ.pop("ELEVENLABS_API_KEY", None)
            else:
                os.environ["ELEVENLABS_API_KEY"] = FAKE_KEY
                FakeElevenLabs.mode = mode
            began = time.monotonic()
            played, sent, events = run_speak({"tts_backend": "elevenlabs"})
            took = time.monotonic() - began
            errors = [e for e in events if e["event"] == "tts_error"]
            ok = (
                said_by_say(played, sent)
                and len(errors) == 1
                and errors[0].get("backend") == "elevenlabs"
                and errors[0].get("fallback") == "say"
                and reason in errors[0].get("error", "")
                and FAKE_KEY not in json.dumps(errors)
            )
            check(f"{label}: `say` spoke, tts_error logged ({took:.1f}s)", ok,
                  {"played": [(len(a), r) for a, r in played], "sent": sent, "errors": errors})

        os.environ["ELEVENLABS_API_KEY"] = FAKE_KEY
        server.shutdown()
        server.server_close()
        played, sent, events = run_speak({"tts_backend": "elevenlabs"})
        errors = [e for e in events if e["event"] == "tts_error"]
        check("connection refused: `say` spoke, tts_error logged",
              said_by_say(played, sent) and len(errors) == 1 and "request failed" in errors[0]["error"], errors)

        played, sent, events = run_speak({"tts_backend": "festival"})
        errors = [e for e in events if e["event"] == "tts_error"]
        check("unknown backend: `say` spoke, tts_error 'unknown backend'",
              said_by_say(played, sent) and len(errors) == 1 and errors[0]["error"] == "unknown backend", errors)

        print("\nspeak() with tts_backend=say")
        played, sent, events = run_speak({"tts_backend": "say"})
        check("plain `say`: spoken, no tts_error, backend=say", said_by_say(played, sent) and not events, (events, sent))
        played, sent, events = run_speak({"tts_backend": "say", "tts_voice": "Eddy (English (US))"})
        check("listed `say` voice used", said_by_say(played, sent) and not events
              and fake_say.renders()[-1]["argv"][1:3] == ["-v", "Eddy (English (US))"], events)
        played, sent, events = run_speak({"tts_backend": "say", "tts_voice": "Nobody"})
        errors = [e for e in events if e["event"] == "tts_error"]
        check("unlisted `say` voice: system voice spoke, tts_error names say",
              said_by_say(played, sent) and len(errors) == 1 and errors[0]["backend"] == "say"
              and "not installed" in errors[0]["error"] and "-v" not in fake_say.renders()[-1]["argv"], errors)

        for label, settings in [("elevenlabs (no key) then `say` fails", {"tts_backend": "elevenlabs"}),
                                ("`say` itself fails", {"tts_backend": "say"})]:
            os.environ.pop("ELEVENLABS_API_KEY", None)
            fake_say.mode = "exit"
            played, sent, events = run_speak(settings)
            fake_say.mode = "ok"
            errors = [e for e in events if e["event"] == "tts_error"]
            check(f"{label}: silent, no `speaking` started, tts_error logged",
                  not played and not [m for m in sent if m.get("state") == "started"]
                  and errors and "exit 1" in errors[-1].get("error", ""), (sent, errors))

        print("\nPiper (optional)")
        if piper_installed:
            played, sent, events = run_speak({"tts_backend": "piper"})
            check("plain Piper: spoken, no tts_error, backend=piper",
                  len(played) == 1 and not events and sent[0].get("backend") == "piper", (events, sent))
            played, sent, events = run_speak({"tts_backend": "piper", "tts_voice": "xx_XX-not_a_voice-low"})
            errors = [e for e in events if e["event"] == "tts_error"]
            check("unknown Piper voice: `say` spoke, tts_error names piper",
                  said_by_say(played, sent) and len(errors) == 1 and errors[0]["backend"] == "piper"
                  and "xx_XX-not_a_voice-low" in errors[0]["error"], errors)
        else:
            print("  (piper not installed in this venv: the installed-Piper scenarios are skipped)")
        played, sent, events = run_speak({"tts_backend": "piper", "tts_voice": "Eddy (English (US))"})
        errors = [e for e in events if e["event"] == "tts_error"]
        check("a macOS-style name as a Piper voice is refused before any file path",
              said_by_say(played, sent) and errors and errors[0]["backend"] == "piper"
              and ("invalid piper voice" in errors[0]["error"] or "not installed" in errors[0]["error"]), errors)

        # Simulate a build without Piper: block the import in this process.
        saved_module = sys.modules.get("piper", "absent")
        sys.modules["piper"] = None
        tts_backends.BACKENDS["piper"] = tts_backends.PiperBackend()  # no voice cached from above
        try:
            check("piper blocked: available() is False", tts_backends.BACKENDS["piper"].available() is False)
            check("piper blocked: availability() says so", tts.availability().get("piper") is False, tts.availability())
            played, sent, events = run_speak({"tts_backend": "piper"})
            errors = [e for e in events if e["event"] == "tts_error"]
            check("piper blocked + selected: `say` spoke, tts_error 'not installed'",
                  said_by_say(played, sent) and len(errors) == 1 and errors[0]["backend"] == "piper"
                  and "not installed" in errors[0]["error"], errors)
            wav = tts.synthesize("Hey Jarvis.")
            check("piper blocked: tts.synthesize() (voice_sim's clips) uses `say`",
                  wav[:4] == b"RIFF" and fake_say.renders()[-1]["input"] == "Hey Jarvis.")
        finally:
            if saved_module == "absent":
                sys.modules.pop("piper", None)
            else:
                sys.modules["piper"] = saved_module
        check_piper_absent_fresh_interpreter()

        check_say_backend(FakeSay())

        print("\none real `say -o` render (to a temp file; nothing is played)")
        real = tts_backends.MacSayBackend()
        audio, rate = real.synthesize("Hello.")
        check("real say: 22050 Hz int16 samples, non-trivial length",
              rate == 22050 and audio.dtype == np.int16 and len(audio) > 2000, (rate, audio.dtype, len(audio)))

        print("\nsettings validation (temp file)")
        voice_settings.load = real_load
        real_file = voice_settings.SETTINGS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            voice_settings.SETTINGS_FILE = Path(tmp) / "voice_settings.json"
            check("defaults: elevenlabs, null voice",
                  voice_settings.load()["tts_backend"] == "elevenlabs" and voice_settings.load()["tts_voice"] is None)
            for partial, should_pass in [
                ({"tts_backend": "say", "tts_voice": "Eddy (English (US))"}, True),
                ({"tts_backend": "say", "tts_voice": "Amélie"}, True),
                ({"tts_backend": "piper"}, True),
                ({"tts_voice": "Albert\n"}, False),
                ({"tts_voice": " Albert"}, False),
                ({"tts_voice": "a.b"}, False),
                ({"tts_backend": "elevenlabs", "tts_voice": "JBFqnCBsd6RMkjVDRZzb"}, True),
                ({"tts_voice": None}, True),
                ({"tts_backend": "openai"}, False),
                ({"tts_backend": None}, False),
                ({"tts_voice": "../../etc/passwd"}, False),
                ({"tts_voice": "a/b"}, False),
                ({"tts_voice": ""}, False),
                ({"tts_voice": 5}, False),
                ({"elevenlabs_api_key": FAKE_KEY}, False),
            ]:
                _, error = voice_settings.update(partial)
                check(f"{json.dumps(partial)} -> {'accepted' if should_pass else 'rejected'}",
                      (error is None) == should_pass, error)
            stored = voice_settings.SETTINGS_FILE.read_text()
            check("file holds tts settings and no key",
                  '"tts_backend": "elevenlabs"' in stored and FAKE_KEY not in stored, stored)
        voice_settings.SETTINGS_FILE = real_file
    finally:
        voice_settings.load = real_load
        tts_backends.BACKENDS.clear()
        tts_backends.BACKENDS.update(real_backends)
        if saved_key is None:
            os.environ.pop("ELEVENLABS_API_KEY", None)
        else:
            os.environ["ELEVENLABS_API_KEY"] = saved_key

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
