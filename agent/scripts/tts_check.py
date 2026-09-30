"""Check the swappable TTS backends without a real ElevenLabs key.

    cd agent
    ./.venv/bin/python scripts/tts_check.py

A fake ElevenLabs server (http.server on a spare localhost port) stands in
for api.elevenlabs.io. It records every request and answers according to the
scenario: PCM audio, a 401/500 with the API's JSON error shape, a 200 that is
JSON instead of audio, or a response slower than the client's read timeout.

Checked:
  - the request shape: POST /v1/text-to-speech/{voice_id}?output_format=pcm_24000,
    xi-api-key header, JSON body {text, model_id}
  - decoding: little-endian int16 PCM -> the exact samples, at 24000 Hz
  - the real speak() path with tts_backend=elevenlabs: the fake player gets
    the ElevenLabs samples, `speaking` says backend=elevenlabs
  - fallbacks -- no key, 401, 500, JSON-not-audio, timeout, connection
    refused, a bad Piper voice: each is spoken by Piper's default voice and
    logs tts_error naming the backend that failed
  - settings validation: tts_backend / tts_voice accepted and rejected as
    they should be, and nothing key-shaped is ever written to the file
  - availability() is a boolean that follows the env var

Nothing is played through the speakers, events.jsonl is not written to
(log_event is captured), and voice_settings.json is never touched (a temp
file stands in for it).
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
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


def main():
    real_load = voice_settings.load
    tts.log_event = lambda event, **fields: logged.append({"event": event, **fields})
    saved_key = os.environ.pop("ELEVENLABS_API_KEY", None)

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeElevenLabs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    fake = tts_backends.ElevenLabsBackend(base_url=base, timeout=(2, 1))
    tts_backends.BACKENDS["elevenlabs"] = fake
    piper_rate = tts_backends.BACKENDS["piper"].synthesize("warm up")[1]
    print(f"fake ElevenLabs at {base}; Piper default voice plays at {piper_rate} Hz\n")

    try:
        print("availability")
        check("no key -> elevenlabs unavailable", tts.availability() == {"piper": True, "elevenlabs": False},
              tts.availability())
        os.environ["ELEVENLABS_API_KEY"] = FAKE_KEY
        avail = tts.availability()
        check("key set -> elevenlabs available, booleans only",
              avail == {"piper": True, "elevenlabs": True} and FAKE_KEY not in json.dumps(avail), avail)

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

        print("\nfallbacks to Piper")
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
            started = [m for m in sent if m.get("state") == "started"]
            errors = [e for e in events if e["event"] == "tts_error"]
            ok = (
                len(played) == 1
                and played[0][1] == piper_rate
                and started and started[0].get("backend") == "piper"
                and len(errors) == 1
                and errors[0].get("backend") == "elevenlabs"
                and errors[0].get("fallback") == "piper"
                and reason in errors[0].get("error", "")
                and FAKE_KEY not in json.dumps(errors)
            )
            check(f"{label}: Piper spoke, tts_error logged ({took:.1f}s)", ok,
                  {"played": [(len(a), r) for a, r in played], "started": started, "errors": errors})

        os.environ["ELEVENLABS_API_KEY"] = FAKE_KEY
        server.shutdown()
        server.server_close()
        played, sent, events = run_speak({"tts_backend": "elevenlabs"})
        errors = [e for e in events if e["event"] == "tts_error"]
        check("connection refused: Piper spoke, tts_error logged",
              len(played) == 1 and played[0][1] == piper_rate and len(errors) == 1
              and "request failed" in errors[0]["error"], errors)

        played, sent, events = run_speak({"tts_backend": "piper", "tts_voice": "xx_XX-not_a_voice-low"})
        errors = [e for e in events if e["event"] == "tts_error"]
        check("unknown Piper voice: default Piper voice spoke, tts_error logged",
              len(played) == 1 and played[0][1] == piper_rate and len(errors) == 1
              and errors[0]["backend"] == "piper"
              and "xx_XX-not_a_voice-low" in errors[0]["error"], errors)

        played, sent, events = run_speak({"tts_backend": "piper"})
        check("plain Piper: spoken, no tts_error, backend=piper",
              len(played) == 1 and not events and sent[0].get("backend") == "piper", (events, sent))

        print("\nsettings validation (temp file)")
        voice_settings.load = real_load
        real_file = voice_settings.SETTINGS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            voice_settings.SETTINGS_FILE = Path(tmp) / "voice_settings.json"
            check("defaults: piper, null voice",
                  voice_settings.load()["tts_backend"] == "piper" and voice_settings.load()["tts_voice"] is None)
            for partial, should_pass in [
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
        if saved_key is None:
            os.environ.pop("ELEVENLABS_API_KEY", None)
        else:
            os.environ["ELEVENLABS_API_KEY"] = saved_key

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
