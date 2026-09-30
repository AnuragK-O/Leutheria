"""Check of the overlay client role: hello, mirroring, and overlay confirms.

    cd agent
    LEUTHERIA_VOICE_SOURCE=/path/to/silent.wav ./.venv/bin/python scripts/overlay_protocol_check.py [--port 8788]

Part 1 runs the real server (`python -m agent`'s main()) on a side port --
never 8765 -- with the mic replaced by LEUTHERIA_VOICE_SOURCE (required; a
silent WAV is enough), and with TTS playback and synthesis stubbed in that
process so the spoken confirmation prompt is neither heard nor sent to a
cloud voice. Two websocket clients: a "UI" (never says hello, like the
Electron app) and an "overlay" ({"type":"hello","role":"overlay"}, like the
Qt overlay), connected in both orders. It proves:

  - the overlay never becomes primary: a manual-dispatch run_command's
    confirmation_required and tool_result go to the UI; the overlay gets a
    mirrored confirmation_required (and the spoken prompt's `speaking`) but
    never the tool_result
  - the overlay gets voice broadcasts (session_started / voice_state)
  - a `confirm` from the overlay resolves the UI's pending confirmation, and
    confirmation_resolved {"by":"overlay"} reaches both, once each
  - a click in the UI (a UI `confirm`) settles the overlay's card with
    confirmation_resolved {"by":"ui"}, and the UI isn't sent one (as before)
  - the overlay can't run commands, and garbage doesn't drop its connection

Part 2 is in-process with fake sockets, for the voice-session sends the
silent WAV can't produce (transcript, response, dismissal decline).

Needs no API key: nothing here reaches the LLM. Writes to the real
logs/events.jsonl, like the other end-to-end checks. Exits non-zero on the
first failed check.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

import websockets  # noqa: E402

# Runs the server exactly as __main__.py does, after stubbing TTS output.
BOOTSTRAP = """
from dotenv import load_dotenv
load_dotenv()
import asyncio
import numpy as np
from agent.core import tts
tts.set_player(lambda audio, rate: None)
tts._synthesize_reply = lambda text: (np.zeros(2205, dtype=np.int16), 22050, "check-stub")
from agent.core.server import main
asyncio.run(main())
"""

failures = 0


def check(ok: bool, label: str) -> None:
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        failures += 1
        raise SystemExit(f"FAILED: {label}")


class Client:
    """A websocket client that records everything it receives."""

    def __init__(self, name: str, ws):
        self.name = name
        self.ws = ws
        self.messages: list = []
        self._task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            async for raw in self.ws:
                self.messages.append(json.loads(raw))
        except websockets.ConnectionClosed:
            pass

    @classmethod
    async def connect(cls, name: str, url: str, overlay: bool) -> "Client":
        ws = await websockets.connect(url, max_size=20 * 1024 * 1024)
        client = cls(name, ws)
        if overlay:
            await client.send({"type": "hello", "role": "overlay"})
        return client

    async def send(self, payload) -> None:
        await self.ws.send(payload if isinstance(payload, str) else json.dumps(payload))

    def of_type(self, kind: str, **match) -> list:
        return [m for m in self.messages if m.get("type") == kind and all(m.get(k) == v for k, v in match.items())]

    async def wait_for(self, kind: str, timeout: float = 20, **match) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.of_type(kind, **match)
            if found:
                return found[-1]
            await asyncio.sleep(0.05)
        raise SystemExit(f"FAILED: {self.name} never got {kind} {match or ''}")

    async def close(self):
        await self.ws.close()
        await self._task


def start_server(port: int) -> subprocess.Popen:
    if not os.environ.get("LEUTHERIA_VOICE_SOURCE"):
        raise SystemExit("set LEUTHERIA_VOICE_SOURCE to a WAV (silence is fine) so the mic is never opened")
    env = {**os.environ, "LEUTHERIA_PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, "-c", BOOTSTRAP], cwd=AGENT_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if "AGENT_READY" in line:
            print(f"server: {line.strip()} (pid {proc.pid})")
            return proc
        if not line and proc.poll() is not None:
            break
    proc.kill()
    raise SystemExit("server did not start")


async def confirmation_round(ui: Client, overlay: Client, label: str) -> None:
    """A run_command that needs confirmation, answered from the overlay."""
    cmd = f"echo overlay-check-{uuid.uuid4().hex[:8]}"  # unique: never already trusted
    await ui.send({"tool": "run_command", "args": {"cmd": cmd}})
    required = await ui.wait_for("confirmation_required")
    check(required["args"].get("cmd") == cmd, f"{label}: confirmation_required went to the UI")
    mirrored = await overlay.wait_for("confirmation_required", id=required["id"])
    check(mirrored["tool"] == "run_command" and mirrored["args"] == required["args"],
          f"{label}: overlay got a mirrored confirmation_required with the same id/tool/args")
    await overlay.wait_for("speaking", state="started")
    check(True, f"{label}: overlay got the spoken prompt's speaking(started)")

    await overlay.send({"type": "confirm", "id": required["id"], "approved": True})
    result = await ui.wait_for("tool_result", timeout=15)
    check(result["result"].get("ok") is True and "overlay-check" in result["result"].get("stdout", ""),
          f"{label}: the overlay's confirm resolved the UI's pending confirmation (command ran)")
    await ui.wait_for("confirmation_resolved", id=required["id"])
    await overlay.wait_for("confirmation_resolved", id=required["id"])
    for client in (ui, overlay):
        resolved = client.of_type("confirmation_resolved", id=required["id"])
        check(len(resolved) == 1 and resolved[0]["by"] == "overlay" and resolved[0]["approved"] is True,
              f"{label}: {client.name} got confirmation_resolved by=overlay exactly once")
    check(not overlay.of_type("tool_result"), f"{label}: the command's result was not sent to the overlay")


async def part1(port: int) -> None:
    url = f"ws://127.0.0.1:{port}"
    proc = start_server(port)
    try:
        # --- order 1: UI first, then overlay -------------------------------
        print("order 1: UI connects first, then the overlay")
        ui = await Client.connect("ui", url, overlay=False)
        overlay = await Client.connect("overlay", url, overlay=True)
        await asyncio.sleep(0.3)
        check(bool(ui.of_type("voice_state")) and bool(overlay.of_type("voice_state")),
              "both got the voice_state snapshot on connect")

        await confirmation_round(ui, overlay, "order 1")

        # A click in the UI: the overlay's card must settle too.
        ui.messages.clear()
        overlay.messages.clear()
        await ui.send({"tool": "run_command", "args": {"cmd": f"echo ui-decline-{uuid.uuid4().hex[:8]}"}})
        required = await ui.wait_for("confirmation_required")
        await overlay.wait_for("confirmation_required", id=required["id"])
        await ui.send({"type": "confirm", "id": required["id"], "approved": False})
        result = await ui.wait_for("tool_result")
        check(result["result"].get("ok") is False and "declined" in result["result"].get("error", ""),
              "a UI decline still resolves its own confirmation")
        resolved = await overlay.wait_for("confirmation_resolved", id=required["id"])
        check(resolved["by"] == "ui" and resolved["approved"] is False,
              "overlay got confirmation_resolved by=ui for the UI's click")
        await asyncio.sleep(0.3)
        check(not ui.of_type("confirmation_resolved"), "the UI was not sent a resolution for its own click (as before)")

        # Voice broadcasts reach the overlay.
        overlay.messages.clear()
        await ui.send({"type": "voice_session", "action": "start", "request_id": "s1"})
        await ui.wait_for("control_result", request_id="s1")
        await overlay.wait_for("session_started")
        await overlay.wait_for("voice_state", session=True)
        check(True, "overlay got session_started and voice_state(session=true) broadcasts")
        await ui.send({"type": "voice_session", "action": "end", "request_id": "s2"})
        await overlay.wait_for("session_ended")
        check(True, "overlay got session_ended")

        # An overlay is a view: no commands, garbage tolerated, stale ids ignored.
        overlay.messages.clear()
        ui.messages.clear()
        await overlay.send({"tool": "run_command", "args": {"cmd": "echo should-not-run"}})
        error = await overlay.wait_for("error")
        check("overlay" in error["text"], "a command from the overlay is refused with an error")
        await overlay.send("5")
        await overlay.send("not json")
        await overlay.send({"type": "confirm", "id": "no-such-id", "approved": True})
        await asyncio.sleep(0.3)
        check(len(overlay.of_type("error")) == 3, "garbage from the overlay gets errors, not a dropped connection")
        check(not ui.of_type("confirmation_required") and not ui.of_type("confirmation_resolved"),
              "none of that reached the UI")
        await overlay.send({"type": "inventory", "request_id": "inv"})
        inv = await overlay.wait_for("control_result", request_id="inv")
        check("tools" in inv, "control messages still work from an overlay (connection alive)")

        await overlay.close()
        await ui.close()

        # --- order 2: overlay first, then UI -------------------------------
        print("order 2: the overlay connects first, then the UI")
        overlay = await Client.connect("overlay", url, overlay=True)
        await asyncio.sleep(0.2)
        ui = await Client.connect("ui", url, overlay=False)
        await asyncio.sleep(0.2)
        await confirmation_round(ui, overlay, "order 2")

        # A second overlay connecting last must not take over as primary.
        late = await Client.connect("late-overlay", url, overlay=True)
        await asyncio.sleep(0.2)
        ui.messages.clear()
        overlay.messages.clear()
        await confirmation_round(ui, overlay, "order 2 + a later overlay")
        check(bool(late.of_type("confirmation_required")) and bool(late.of_type("confirmation_resolved", by="overlay")),
              "the later overlay got the mirror too, and didn't become primary")
        await late.close()

        # --- a client without hello is unchanged ---------------------------
        print("no hello: a lone UI connection behaves as before")
        await overlay.close()
        ui.messages.clear()
        await ui.send({"tool": "run_command", "args": {"cmd": f"echo plain-{uuid.uuid4().hex[:8]}"}})
        required = await ui.wait_for("confirmation_required")
        await ui.send({"type": "confirm", "id": required["id"], "approved": True})
        result = await ui.wait_for("tool_result")
        await asyncio.sleep(0.3)
        check(result["result"].get("ok") is True and not ui.of_type("confirmation_resolved"),
              "confirm round trip works, with no confirmation_resolved (exactly as before)")
        await ui.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"server pid {proc.pid} stopped")


class FakeSocket:
    def __init__(self):
        self.messages = []

    async def send(self, raw):
        self.messages.append(json.loads(raw))


async def part2() -> None:
    """Voice-session sends, in process (the silent WAV never produces speech)."""
    print("in-process: voice-session mirroring")
    from agent.core import server

    ui_raw, overlay = FakeSocket(), FakeSocket()
    ui = server._MirroringSocket(ui_raw)
    pending: dict = {}
    server._connections[:] = [(ui, pending, {}), (overlay, {}, {})]
    server._overlays.add(overlay)
    try:
        check(server._primary()[0] is ui, "the overlay, though newest, is not the primary")

        # An unclear remark while nothing is pending: shown, not run.
        await server.answer_confirmation_by_voice("the weather is nice")
        check(overlay.messages == [{"type": "transcript", "text": "the weather is nice", "origin": "voice_session"}],
              "a voice-session transcript is mirrored to the overlay")

        overlay.messages.clear()
        await server._send(ui, {"type": "response", "text": "hi", "origin": "voice_session"})
        await server._send(ui, {"type": "response", "text": "typed reply"})
        await server._send(ui, {"type": "transcript", "text": "ptt", "origin": "push_to_talk"})
        await server._send(ui, {"type": "skill_proposed", "id": "x"})
        check(overlay.messages == [{"type": "response", "text": "hi", "origin": "voice_session"}],
              "only the voice-session response is mirrored (not typed replies, push-to-talk or proposals)")
        check(len(ui_raw.messages) == 5, "the UI got every one of them")

        # Session dismissed with a confirmation open (BUGS.md #22 path).
        overlay.messages.clear()
        future = asyncio.get_running_loop().create_future()
        pending["c1"] = future
        await server.decline_pending_by_dismissal()
        check(future.result()["approved"] is False, "dismissal declined the pending confirmation")
        check(overlay.messages == [{"type": "confirmation_resolved", "id": "c1", "approved": False, "by": "voice"}],
              "the dismissal's confirmation_resolved is mirrored to the overlay")

        # Broadcasts reach the overlay once (not again through the mirror).
        overlay.messages.clear()
        await server.broadcast({"type": "voice_state", "state": "idle", "session": False})
        check(len(overlay.messages) == 1, "a broadcast reaches the overlay exactly once")

        # No UI connected: a voice command's transcript still reaches the overlay.
        server._connections[:] = [(overlay, {}, {})]
        overlay.messages.clear()
        check(server._primary()[0] is None, "with only an overlay connected there is no primary")
        await server.answer_confirmation_by_voice("anyone there")
        check(overlay.messages and overlay.messages[0]["type"] == "transcript",
              "with no UI, the overlay still gets the voice-session transcript")
    finally:
        server._connections.clear()
        server._overlays.clear()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    if args.port == 8765:
        raise SystemExit("refusing to use 8765, the real agent's port")
    asyncio.run(part1(args.port))
    asyncio.run(part2())
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
