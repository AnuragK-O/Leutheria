"""Check that the agent's WebSocket is authenticated (security review C1).

    cd agent
    ./.venv/bin/python scripts/ws_auth_check.py [--port 8789]

Starts the real server (`python -m agent`) several times on a side port --
never 8765 -- with the mic replaced by a silent WAV this script generates in
its own temp directory, and talks to it with raw sockets, so what is asserted
is the HTTP status of the opening handshake itself, not a client library's
summary of it. It proves:

  - no token, a wrong token, a near-miss, the wrong scheme, a token in the
    query string, two Authorization headers: all refused with 401 *before*
    the upgrade (never a 101)
  - the right token is accepted
  - the right token plus an Origin header -- any value, including "null",
    file:// and the server's own address -- is refused with 403
  - the token never appears on the agent's stdout/stderr or in
    logs/events.jsonl, and a flood of bad handshakes doesn't flood the log
  - LEUTHERIA_TOKEN is gone from the agent's environment once read, so a
    command the agent runs can't see it
  - standalone (no LEUTHERIA_TOKEN): the agent generates a token, writes it
    to a 0600 file in a 0700 directory, requires it, and makes a new one on
    every start; it refuses to start if that directory is writable by others
    or if LEUTHERIA_TOKEN is too short to be a secret
  - the bare {"tool": ...} manual dispatch is refused unless the agent was
    started with LEUTHERIA_MANUAL_DISPATCH=1

Needs no API key: nothing here reaches the LLM. The one command it runs (with
manual dispatch enabled) prints a fixed word. TTS is stubbed in that server so
the spoken confirmation prompt is neither heard nor sent to a cloud voice.
Writes to logs/events.jsonl like the other end-to-end checks. Exits non-zero
on the first failed check.
"""

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

import websockets  # noqa: E402

from agent.core import ws_auth  # noqa: E402
from agent.core.logging_util import LOG_FILE  # noqa: E402

# `python -m agent`, but with TTS output stubbed (see overlay_protocol_check).
STUBBED_TTS = """
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

passed = 0


def check(ok: bool, label: str) -> None:
    global passed
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        raise SystemExit(f"FAILED: {label}")
    passed += 1


class Server:
    """One run of the agent. Keeps everything it printed, to search for the
    token afterwards."""

    def __init__(self, port: int, env: dict, stub_tts: bool = False):
        self.port = port
        self.output = ""
        argv = [sys.executable, "-c", STUBBED_TTS] if stub_tts else [sys.executable, "-m", "agent"]
        self.proc = subprocess.Popen(
            argv, cwd=AGENT_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.ready_line = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            self.output += line
            if line.startswith("AGENT_READY"):
                self.ready_line = line.strip()
                return
            if not line and self.proc.poll() is not None:
                return  # exited without becoming ready; the caller decides if that's a failure

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.output += self.proc.stdout.read() or ""


def handshake(port: int, headers: dict = None, path: str = "/", upgrade: bool = True, extra: list = ()) -> tuple:
    """Send one HTTP request and return (status, response headers as text).
    Raw, so the result is exactly what the server put on the wire."""
    lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
    if upgrade:
        lines += ["Upgrade: websocket", "Connection: Upgrade",
                  "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version: 13"]
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    lines += list(extra)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    head = data.split(b"\r\n\r\n")[0].decode("latin-1")
    return int(head.split()[1]), head


def base_env(port: int, wav: Path, runtime: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in (ws_auth.TOKEN_ENV, "LEUTHERIA_MANUAL_DISPATCH")}
    env.update({
        "LEUTHERIA_PORT": str(port),
        "LEUTHERIA_VOICE_SOURCE": str(wav),
        # Never the user's real ~/Library/Application Support/Leutheria.
        ws_auth.RUNTIME_DIR_ENV: str(runtime),
    })
    env.update(extra)
    return env


def log_since(offset: int) -> str:
    if not LOG_FILE.exists():
        return ""
    with LOG_FILE.open() as f:
        f.seek(offset)
        return f.read()


async def recv_until(ws, predicate, timeout: float):
    """The first message matching `predicate`, or None after `timeout`."""
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        try:
            message = json.loads(await asyncio.wait_for(ws.recv(), max(0.05, deadline - time.monotonic())))
        except asyncio.TimeoutError:
            break
        seen.append(message)
        if predicate(message):
            return message, seen
    return None, seen


# --- 1. a token from the environment (how Electron starts the agent) ---------

def env_token_checks(port: int, wav: Path, tmp: Path) -> None:
    print("\n# token from LEUTHERIA_TOKEN (how the app starts the agent)")
    token = secrets.token_hex(32)
    other = secrets.token_hex(32)
    runtime = tmp / "runtime-env"
    log_start = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    server = Server(port, base_env(port, wav, runtime, **{ws_auth.TOKEN_ENV: token}))
    try:
        check(server.ready_line == f"AGENT_READY ws://127.0.0.1:{port} auth=token-env",
              f"started, and says which mode without the token: {server.ready_line!r}")
        check(not runtime.exists(), "no token file is written when the token came from the environment")

        status, head = handshake(port)
        check(status == 401 and "WWW-Authenticate: Bearer" in head, "no token -> 401 before the upgrade")
        check("Upgrade" not in head and "Sec-WebSocket-Accept" not in head, "...and nothing about the response is a WebSocket upgrade")
        check(handshake(port, {"Authorization": f"Bearer {other}"})[0] == 401, "wrong token -> 401")
        check(handshake(port, {"Authorization": f"Bearer {token[:-1]}"})[0] == 401, "token missing its last character -> 401")
        check(handshake(port, {"Authorization": f"Bearer {token}0"})[0] == 401, "token with an extra character -> 401")
        check(handshake(port, {"Authorization": "Bearer "})[0] == 401, "empty bearer token -> 401")
        check(handshake(port, {"Authorization": token})[0] == 401, "the token without the Bearer scheme -> 401")
        check(handshake(port, {"Authorization": f"Basic {token}"})[0] == 401, "the token under another scheme -> 401")
        check(handshake(port, path=f"/?token={token}")[0] == 401, "the token in the query string -> 401 (headers only)")
        check(handshake(port, {"Sec-WebSocket-Protocol": token})[0] == 401,
              "the token as a subprotocol (the one header a browser can set) -> 401")
        check(handshake(port, {"Authorization": f"Bearer {other}"}, extra=[f"Authorization: Bearer {token}"])[0] == 401,
              "two Authorization headers, one of them right -> 401")
        check(handshake(port, upgrade=False)[0] == 401, "a plain HTTP GET (not a WebSocket request) -> 401, nothing is served")

        status, head = handshake(port, {"Authorization": f"Bearer {token}"})
        check(status == 101 and "Sec-WebSocket-Accept" in head, "right token -> 101, upgraded")
        check("Server:" not in head, "the response doesn't advertise what the server is")

        for origin in ("https://evil.example", "null", "file://", f"http://127.0.0.1:{port}", "http://localhost", ""):
            status, _ = handshake(port, {"Authorization": f"Bearer {token}", "Origin": origin})
            check(status == 403, f"right token + Origin: {origin!r} -> 403")
        check(handshake(port, {"Origin": "https://evil.example"})[0] == 403, "Origin and no token -> 403")

        # A page retrying in a loop must not become a line per attempt.
        for _ in range(200):
            handshake(port, {"Origin": "https://evil.example"})
        for _ in range(200):
            handshake(port, {"Authorization": f"Bearer {other}"})

        async def as_client():
            url = f"ws://127.0.0.1:{port}"
            try:
                await websockets.connect(url)
                rejected = False
            except websockets.exceptions.InvalidStatus as e:
                rejected = e.response.status_code == 401
            check(rejected, "websockets client with no token: InvalidStatus 401, no connection object")
            try:
                await websockets.connect(url, additional_headers=ws_auth.bearer(token), origin="https://evil.example")
                rejected = False
            except websockets.exceptions.InvalidStatus as e:
                rejected = e.response.status_code == 403
            check(rejected, "websockets client with the token and an Origin: InvalidStatus 403")

            async with websockets.connect(url, additional_headers=ws_auth.bearer(token)) as ws:
                await ws.send(json.dumps({"type": "inventory", "request_id": "inv"}))
                reply, _ = await recv_until(ws, lambda m: m.get("request_id") == "inv", 15)
                check(bool(reply) and reply.get("ok") and len(reply.get("tools", [])) > 0,
                      "websockets client with the token: connected, and the server answers it")

                # Design point 6: the bare manual dispatch is off by default.
                for payload in ({"tool": "run_command", "args": {"cmd": "echo should-not-run"}},
                                {"type": "command", "tool": "list_dir", "args": {"path": "~"}}):
                    await ws.send(json.dumps(payload))
                    reply, seen = await recv_until(ws, lambda m: m.get("type") in ("error", "tool_result", "confirmation_required"), 10)
                    check(bool(reply) and reply["type"] == "error" and "manual tool dispatch is disabled" in reply["text"],
                          f"manual dispatch of {payload['tool']} is refused (no LEUTHERIA_MANUAL_DISPATCH)")
                _, seen = await recv_until(ws, lambda m: False, 1.0)
                check(not any(m.get("type") in ("confirmation_required", "tool_result") for m in seen),
                      "...and nothing ran or asked for confirmation")

        asyncio.run(as_client())
    finally:
        server.stop()

    log = log_since(log_start)
    events = [json.loads(line) for line in log.splitlines() if line.strip()]
    check(token not in server.output, "the token never appears on the agent's stdout or stderr")
    check(token not in log, "the token never appears in logs/events.jsonl")
    check(other not in log and other not in server.output, "nor does a wrong token that was presented")
    check("evil.example" not in log, "nor the Origin a refused client claimed")
    started = [e for e in events if e["type"] == "ws_auth"]
    check(len(started) == 1 and started[0]["source"] == "env" and started[0]["token_file"] is None
          and started[0]["manual_dispatch"] is False, "startup is logged as ws_auth {source: env}")
    rejected = [e for e in events if e["type"] == "ws_rejected"]
    reasons = {e["reason"] for e in rejected}
    check(reasons == {"origin", "no_token", "bad_token"}, f"refusals are logged by reason only: {sorted(reasons)}")
    check(len(rejected) <= 12, f"~430 refused handshakes produced {len(rejected)} log lines (rate-limited)")
    check(any(e["type"] == "manual_dispatch_refused" for e in events), "the refused manual dispatch is logged")
    check(not any(e["type"] == "tool_call" for e in events), "no tool_call was logged: nothing ran")


# --- 2. standalone: no token configured still means authenticated ----------------

def standalone_checks(port: int, wav: Path, tmp: Path) -> None:
    print("\n# standalone, no LEUTHERIA_TOKEN (python -m agent)")
    runtime = tmp / "runtime-standalone"
    token_file = runtime / f"agent-{port}.token"
    log_start = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    server = Server(port, base_env(port, wav, runtime))
    try:
        check(server.ready_line == f"AGENT_READY ws://127.0.0.1:{port} auth=token-file:{token_file}",
              f"started, and points at the token file: {server.ready_line!r}")
        check(token_file.is_file() and not token_file.is_symlink(), "the token file exists")
        check(stat.S_IMODE(token_file.stat().st_mode) == 0o600, "the token file is 0600")
        check(stat.S_IMODE(runtime.stat().st_mode) == 0o700, "its directory was created 0700")
        check(ws_auth.token_file_path(port) != token_file, "(the check's server did not use the real per-user directory)")
        first = token_file.read_text().strip()
        check(len(first) == 64 and all(c in "0123456789abcdef" for c in first), "it holds a 256-bit random token")
        check([p.name for p in runtime.iterdir()] == [token_file.name], "no temporary file is left beside it")
        check(handshake(port)[0] == 401, "no token -> 401: standalone is not unauthenticated")
        check(handshake(port, {"Authorization": f"Bearer {secrets.token_hex(32)}"})[0] == 401, "a guessed token -> 401")
        check(handshake(port, {"Authorization": f"Bearer {first}"})[0] == 101, "the token from the file -> accepted")
        check(handshake(port, {"Authorization": f"Bearer {first}", "Origin": "null"})[0] == 403, "the token from the file + Origin -> 403")
    finally:
        server.stop()
    check(first not in server.output, "the generated token is never printed")
    log = log_since(log_start)
    check(first not in log, "nor written to logs/events.jsonl")
    check(any(json.loads(line)["type"] == "ws_auth" and json.loads(line)["source"] == "file"
              for line in log.splitlines() if line.strip()), "startup is logged as ws_auth {source: file}")

    server = Server(port, base_env(port, wav, runtime))
    try:
        second = token_file.read_text().strip()
        check(server.ready_line is not None and second != first, "a restart generates a new token")
        check(stat.S_IMODE(token_file.stat().st_mode) == 0o600, "the replaced file is still 0600")
        check(handshake(port, {"Authorization": f"Bearer {first}"})[0] == 401, "the previous run's token no longer works")
        check(handshake(port, {"Authorization": f"Bearer {second}"})[0] == 101, "the new one does")
    finally:
        server.stop()

    print("\n# refusing to start rather than starting unauthenticated")
    unsafe = tmp / "runtime-unsafe"
    unsafe.mkdir()
    os.chmod(unsafe, 0o777)
    server = Server(port, base_env(port, wav, unsafe))
    server.stop()
    check(server.ready_line is None and server.proc.returncode not in (0, None),
          "a token directory writable by other users: the agent exits instead of serving")
    check("writable by other users" in server.output and not list(unsafe.iterdir()), "...saying why, and writing nothing there")

    link_target = tmp / "elsewhere"
    link_target.mkdir(mode=0o700)
    link = tmp / "runtime-link"
    link.symlink_to(link_target)
    server = Server(port, base_env(port, wav, link))
    server.stop()
    check(server.ready_line is None and not list(link_target.iterdir()), "a token directory that is a symlink: refused")

    server = Server(port, base_env(port, wav, tmp / "runtime-short", **{ws_auth.TOKEN_ENV: "hunter2"}))
    server.stop()
    check(server.ready_line is None and server.proc.returncode not in (0, None),
          "LEUTHERIA_TOKEN too short to be a secret: the agent exits instead of using it")
    check("hunter2" not in server.output, "...without echoing it")
    try:
        handshake(port)
        listening = True
    except OSError:
        listening = False
    check(not listening, "and nothing is left listening on the port")


# --- 3. manual dispatch, when explicitly enabled -----------------------------

def manual_dispatch_checks(port: int, wav: Path, tmp: Path) -> None:
    print("\n# LEUTHERIA_MANUAL_DISPATCH=1 (debugging / check scripts only)")
    token = secrets.token_hex(32)
    log_start = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    env = base_env(port, wav, tmp / "runtime-manual", **{ws_auth.TOKEN_ENV: token, "LEUTHERIA_MANUAL_DISPATCH": "1"})
    server = Server(port, env, stub_tts=True)
    try:
        check(server.ready_line is not None, "started")
        check(handshake(port)[0] == 401, "enabling manual dispatch does not relax authentication")

        async def run():
            async with websockets.connect(f"ws://127.0.0.1:{port}", additional_headers=ws_auth.bearer(token)) as ws:
                # Prints one fixed word; never the environment itself.
                cmd = 'test -z "${LEUTHERIA_TOKEN+x}" && echo token-unset || echo token-set'
                await ws.send(json.dumps({"tool": "run_command", "args": {"cmd": cmd}}))
                required, _ = await recv_until(ws, lambda m: m.get("type") in ("confirmation_required", "tool_result", "error"), 20)
                check(bool(required) and required["type"] == "confirmation_required",
                      "a manual run_command still stops at the confirmation gate")
                await ws.send(json.dumps({"type": "confirm", "id": required["id"], "approved": True, "remember": None}))
                result, _ = await recv_until(ws, lambda m: m.get("type") == "tool_result", 20)
                check(bool(result) and result["result"].get("ok") and result["result"].get("stdout") == "token-unset",
                      "the command the agent ran does not have LEUTHERIA_TOKEN in its environment")

        asyncio.run(run())
    finally:
        server.stop()
    log = log_since(log_start)
    check(token not in log and token not in server.output, "the token is in neither the log nor the output of this run")
    check(any(json.loads(line)["type"] == "ws_auth" and json.loads(line)["manual_dispatch"] is True
              for line in log.splitlines() if line.strip()), "startup logs that manual dispatch is on")


def unit_checks() -> None:
    print("\n# ws_auth, in process")

    class Headers(dict):
        def get_all(self, name):
            return [v for k, v in self.items() if k.lower() == name.lower()]

        def __contains__(self, name):
            return any(k.lower() == str(name).lower() for k in self.keys())

    token = secrets.token_hex(32)
    auth = ws_auth.Auth(token, ws_auth.SOURCE_ENV)
    check(token not in repr(auth) and token not in str(auth) and token not in auth.describe(),
          "an Auth never renders its token (repr/str/describe)")
    check(ws_auth.check_request(auth, Headers(Authorization=f"Bearer {token}")) is None, "check_request accepts the token")
    check(ws_auth.check_request(auth, Headers(Authorization=f"bearer   {token}  ")) is None, "...scheme case and padding don't matter")
    check(ws_auth.check_request(auth, Headers(Authorization="Bearer café" + "x" * 60))[1] == "bad_token",
          "a non-ASCII token is a mismatch, not an exception")
    check(ws_auth.check_request(auth, Headers(Authorization=f"Bearer {token}", Origin="null"))[1] == "origin",
          "Origin is checked before the token")
    for bad in ("", "short", "x" * 31):
        try:
            ws_auth.Auth(bad, ws_auth.SOURCE_ENV)
            refused = False
        except ws_auth.AuthConfigError:
            refused = True
        check(refused, f"an Auth can't be built around a {len(bad)}-character token")
    for not_auth in (None, "", token):
        try:
            ws_auth.make_process_request(not_auth)
            refused = False
        except ws_auth.AuthConfigError:
            refused = True
        check(refused, f"the handshake hook can't be built without an Auth ({type(not_auth).__name__})")
    from agent.core import server
    import inspect
    source = inspect.getsource(server)
    check(source.count("websockets.serve(") == 1 and "process_request=ws_auth.make_process_request(auth)" in source,
          "server.py binds the handler in exactly one place, with the handshake check")
    check(server.HOST == "127.0.0.1", "and only on loopback")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8789)
    args = parser.parse_args()
    if args.port == 8765:
        raise SystemExit("refusing to use 8765, the real agent's port")

    tmp = Path(tempfile.mkdtemp(prefix="leutheria-ws-auth-check-"))
    wav = tmp / "silence.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    try:
        unit_checks()
        env_token_checks(args.port, wav, tmp)
        standalone_checks(args.port, wav, tmp)
        manual_dispatch_checks(args.port, wav, tmp)
    finally:
        os.chmod(tmp / "runtime-unsafe", 0o700) if (tmp / "runtime-unsafe").exists() else None
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nALL {passed} CHECKS PASSED")


if __name__ == "__main__":
    main()
