"""The shared-skill registry end to end, offline: share -> screen -> index ->
sign -> serve -> install -> run, with the real components.

    cd agent
    ./.venv/bin/python scripts/registry_e2e_check.py             # the whole loop, asserted
    ./.venv/bin/python scripts/registry_e2e_check.py --fixture   # for ui-capture.js --registry

The registry side is skill-registry/tools/ exactly as CI runs it
(local_pipeline.py: check_submission -> screen.py -> decide -> record_review
-> build_index -> sign_index -> verify_index), on a temp copy of the repo,
with a key pair from keygen.py. The screener runs with --model-stub (a
wrapper adds it), so no model is called; a submission whose stub verdict is
"flag" must be held for review and stay out of the index. The repo is served
by `python -m http.server` on a spare loopback port.

The agent side is the real server (core/server.py main(), as __main__.py runs
it) in a child process, on a spare port, with its own random LEUTHERIA_TOKEN,
LEUTHERIA_MANUAL_DISPATCH=1, LEUTHERIA_VOICE_SOURCE=<a silent WAV made here>,
the test-only LEUTHERIA_REGISTRY_URL / LEUTHERIA_REGISTRY_TEST_KEY, and HOME
plus every state file (generated/, preferences, trust, voice settings, the
event log) redirected into the temp dir before the server starts -- the
agent's state paths are module constants with no env override, so a
bootstrap sets them, the way overlay_protocol_check.py stubs TTS. TTS
playback is stubbed (no sound) and no .env is loaded (no model can be
called). Everything is driven over the authenticated WebSocket: registry_*
control messages, and the imported skill run by manual dispatch with a
client that answers each confirmation card.

--fixture sets up the same registry and agent, prints one JSON line
{"url", "token", "port", "registry"} and then takes commands on stdin, one
per line, answering "ok <command>" / "error <command>: ...":
    v2       publish a second version of repo_status (an update)
    yank     withdraw repo_status v1
    tamper   change one byte of the served index.json (signature no longer verifies)
    untamper put it back
    quit     stop everything
"""

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
ROOT = AGENT_DIR.parent
REGISTRY = ROOT / "skill-registry"
TOOLS = REGISTRY / "tools"
PY = sys.executable
sys.path.insert(0, str(AGENT_DIR))

import websockets  # noqa: E402

from agent.core import ws_auth  # noqa: E402
from agent.skills import validate  # noqa: E402

# Runs the server as __main__.py does, after pointing every state file into
# the sandbox and stubbing TTS output. No load_dotenv(): no API key, so
# nothing here can reach the model.
BOOTSTRAP = """
import asyncio, os, sys
from pathlib import Path
import numpy as np
sandbox = Path(os.environ["E2E_SANDBOX"])
from agent.core import logging_util
logging_util.LOG_DIR = sandbox / "logs"
logging_util.LOG_FILE = logging_util.LOG_DIR / "events.jsonl"
from agent.core import preferences, trust, voice_settings
preferences.PREFS_FILE = sandbox / "preferences.json"
preferences._load()
trust.TRUST_FILE = sandbox / "trusted_commands.json"
trust._load_permanent_trust()
voice_settings.SETTINGS_FILE = sandbox / "voice_settings.json"
from agent.skills import registry
registry.GENERATED_DIR = sandbox / "generated"
registry.load_generated_skills()
from agent.core import tts
tts.set_player(lambda audio, rate: None)
tts._synthesize_reply = lambda text: (np.zeros(2205, dtype=np.int16), 22050, "check-stub")
from agent.core.server import main
asyncio.run(main())
"""

# local_pipeline.py runs `<screen script> <skill> --catalog .. --out ..`;
# this one is screen.py with --model-stub added (the stub file from the env).
SCREEN_WRAPPER = """
import os, sys
sys.path.insert(0, {screen_dir!r})
import screen
sys.exit(screen.main(sys.argv[1:] + ["--model-stub", os.environ["E2E_SCREEN_STUB"]]))
"""

APPROVE = {"verdict": "approve", "risk": "low", "description_matches_steps": True, "findings": [],
           "summary": "Does what it says."}
FLAG = {"verdict": "flag", "risk": "medium", "description_matches_steps": True,
        "findings": [{"where": "steps[0]", "issue": "unclear why it needs this", "severity": "medium"}],
        "summary": "Probably fine; a person should look."}

passed = 0


def check(condition, label):
    global passed
    if not condition:
        print(f"FAIL  {label}")
        raise SystemExit(1)
    passed += 1
    print(f"ok    {label}")


def spare_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port != 8765:
            return port


def make_skill(name, description, steps, properties=None, required=None):
    definition = {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": properties or {}, **({"required": required} if required else {})},
        "steps": steps,
    }
    definition.update(id=str(uuid.uuid4()), content_hash=validate.content_hash(definition), version=1,
                      requires=validate.requires(definition), created_at=1760000000)
    return definition


REPO_STATUS = make_skill(
    "repo_status", "Show the time, then the git status of the demo project.",
    [{"tool": "get_current_datetime", "args": {}},
     {"tool": "run_command", "args": {"cmd": "git status --short"}}],
)
REPO_STATUS_V2 = make_skill(
    "repo_status", "Show the time, the git status of the demo project, and its last commit.",
    [{"tool": "get_current_datetime", "args": {}},
     {"tool": "run_command", "args": {"cmd": "git status --short"}},
     {"tool": "run_command", "args": {"cmd": "git log -1 --oneline"}}],
)
NOTES_LIST = make_skill(
    "list_notes_folder", "List what is in a folder under Notes.",
    [{"tool": "list_files", "args": {"path": "~/Notes/{folder}"}}],
    properties={"folder": {"type": "string", "description": "Folder under Notes."}}, required=["folder"],
)
HELD = make_skill(
    "time_twice", "Say the time twice.",
    [{"tool": "get_current_datetime", "args": {}}, {"tool": "get_current_datetime", "args": {}}],
)


class Setup:
    """The registry repo (pipeline work dir, keys, http.server) and the
    sandboxed agent, all under one temp dir."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="leutheria_registry_e2e_"))
        self.home = self.tmp / "home"
        self.sandbox = self.tmp / "agent_state"
        self.work = self.tmp / "pipeline"
        self.keys = self.tmp / "keys"
        for d in (self.home, self.sandbox, self.work, self.keys):
            d.mkdir()
        self.procs = []
        self.token = secrets.token_hex(32)

    # --- registry --------------------------------------------------------------
    def keygen(self):
        out = subprocess.run([PY, str(TOOLS / "keygen.py"), "--private-out", str(self.keys / "signing.pem"),
                              "--keys", str(self.keys / "public_keys.json")],
                             capture_output=True, text=True, timeout=60, check=True)
        self.key_entry = json.loads(out.stdout.strip().splitlines()[-1])
        wrapper = self.tmp / "screen_stubbed.py"
        wrapper.write_text(SCREEN_WRAPPER.format(screen_dir=str(REGISTRY / "screen")))
        self.screen_wrapper = wrapper
        return self.key_entry

    def _pipeline(self, *args, stub=None):
        env = dict(os.environ)
        if stub is not None:
            path = self.tmp / f"stub_{uuid.uuid4().hex[:8]}.json"
            path.write_text(json.dumps(stub))
            env["E2E_SCREEN_STUB"] = str(path)
        env.pop("ANTHROPIC_API_KEY", None)
        out = subprocess.run([PY, str(TOOLS / "local_pipeline.py"), *args, "--work", str(self.work),
                              "--leutheria", str(ROOT), "--key-file", str(self.keys / "signing.pem"),
                              "--keys", str(self.keys / "public_keys.json")],
                             capture_output=True, text=True, timeout=300, env=env)
        try:
            return json.loads(out.stdout)
        except ValueError:
            raise SystemExit(f"local_pipeline {args[0]} printed no summary: {out.stderr[-800:]}")

    def submit(self, definition, author, stub):
        path = self.tmp / f"submit_{definition['content_hash'][:12]}.json"
        path.write_text(json.dumps(definition, indent=2) + "\n")
        return self._pipeline("submit", "--skill", str(path), "--author", author,
                              "--screen-script", str(self.screen_wrapper), stub=stub)

    def yank(self, definition):
        return self._pipeline("yank", "--name", definition["name"], "--content-hash", definition["content_hash"],
                              "--reason", "e2e check")

    @property
    def repo(self) -> Path:
        return self.work / "repo"

    def index(self) -> dict:
        return json.loads((self.repo / "index.json").read_text())

    def serve(self):
        self.http_port = spare_port()
        # The work dir, not repo/: a merge replaces repo/ wholesale, and a
        # server rooted inside it would keep serving the deleted directory.
        proc = subprocess.Popen([PY, "-m", "http.server", str(self.http_port), "--bind", "127.0.0.1",
                                 "--directory", str(self.work)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        self.registry_url = f"http://127.0.0.1:{self.http_port}/repo/"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.http_port), timeout=1):
                    return
            except OSError:
                time.sleep(0.1)
        raise SystemExit("http.server did not start")

    def tamper(self, on: bool):
        path = self.repo / "index.json"
        data = path.read_bytes()
        if on:
            self._clean_index = data
            path.write_bytes(data.replace(b'"format": 1', b'"format":  1', 1) if b'"format": 1' in data else data + b" ")
        else:
            path.write_bytes(self._clean_index)

    # --- agent -------------------------------------------------------------------
    def prepare_home(self):
        demo = self.home / "Projects" / "demo"
        demo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(demo)], check=True, timeout=30)
        (demo / "README.md").write_text("demo\n")
        (self.home / "Notes" / "work").mkdir(parents=True)
        (self.home / "Notes" / "work" / "todo.md").write_text("- a\n")
        # A skill "learned" here, to share: a home path that becomes ~, and a
        # personal constant (an email) the preview must flag.
        gen = self.sandbox / "generated"
        gen.mkdir()
        learned = make_skill(
            "client_notes", "Open the client folder and note who to email.",
            [{"tool": "list_files", "args": {"path": f"{self.home}/Documents/Clients"}},
             {"tool": "append_text", "args": {"path": "~/Notes/clients.md", "text": "email bob@example.com"}}],
        )
        learned["source"] = "learned"
        (gen / "client_notes.json").write_text(json.dumps(learned, indent=2))
        wav = self.tmp / "silence.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000)
        self.wav = wav

    def start_agent(self):
        self.agent_port = spare_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith("LEUTHERIA_") and k != "ANTHROPIC_API_KEY"}
        env.update({
            "HOME": str(self.home),
            "E2E_SANDBOX": str(self.sandbox),
            "LEUTHERIA_PORT": str(self.agent_port),
            ws_auth.TOKEN_ENV: self.token,
            "LEUTHERIA_MANUAL_DISPATCH": "1",
            "LEUTHERIA_VOICE_SOURCE": str(self.wav),
            "LEUTHERIA_REGISTRY_URL": self.registry_url,
            "LEUTHERIA_REGISTRY_TEST_KEY": f"{self.key_entry['key_id']}:{self.key_entry['public_key']}",
        })
        # The shared skill runs `git status` in the agent's working directory
        # (run_command has no cwd argument, and `git -C` isn't on the registry's
        # auto-approve allowlist), so that directory must be a repository.
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True, timeout=30)
        proc = subprocess.Popen([PY, "-c", BOOTSTRAP], cwd=self.tmp, env={**env, "PYTHONPATH": str(AGENT_DIR)},
                                stdout=subprocess.PIPE, stderr=open(self.tmp / "agent.stderr", "w"), text=True)
        self.procs.append(proc)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if "AGENT_READY" in line:
                self.agent_url = f"ws://127.0.0.1:{self.agent_port}"
                return
            if not line and proc.poll() is not None:
                break
        raise SystemExit(f"agent did not start: {(self.tmp / 'agent.stderr').read_text()[-1500:]}")

    def stop(self, keep=False):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if not keep:
            shutil.rmtree(self.tmp, ignore_errors=True)


class Client:
    """One authenticated connection. Answers every confirmation card with
    policy(card) -> (approved, remember) and records the cards."""

    def __init__(self, ws, policy):
        self.ws, self.policy = ws, policy
        self.cards, self.messages = [], []
        self._waiters = {}
        self._task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                self.messages.append(m)
                if m.get("type") == "confirmation_required":
                    self.cards.append(m)
                    approved, remember = self.policy(m)
                    await self.ws.send(json.dumps({"type": "confirm", "id": m["id"], "approved": approved, "remember": remember}))
                key = m.get("request_id") if m.get("type") == "control_result" else m.get("type")
                fut = self._waiters.pop(key, None)
                if fut and not fut.done():
                    fut.set_result(m)
        except websockets.ConnectionClosed:
            pass

    async def _await(self, key, payload, timeout=60):
        fut = asyncio.get_running_loop().create_future()
        self._waiters[key] = fut
        await self.ws.send(json.dumps(payload))
        return await asyncio.wait_for(fut, timeout)

    async def control(self, payload):
        rid = uuid.uuid4().hex
        return await self._await(rid, {**payload, "request_id": rid})

    async def run(self, tool, args=None):
        return (await self._await("tool_result", {"tool": tool, "args": args or {}}))["result"]


async def full_loop():
    s = Setup()
    try:
        await _full_loop(s)
    finally:
        s.stop()
    print(f"\nall {passed} checks passed")


async def _full_loop(s: Setup):
    print("# registry: keys, submissions, screening, signed index")
    entry = s.keygen()
    check(len(entry["key_id"]) == 16 and entry["public_key"], "keygen made a key pair")
    r1 = s.submit(REPO_STATUS, "octo-cat", APPROVE)
    check(r1["decision"]["action"] == "merge" and r1.get("verified"), f"repo_status (benign run_command) auto-approved and published: {r1['decision']['reasons']}")
    r2 = s.submit(NOTES_LIST, "mira-k", APPROVE)
    check(r2["decision"]["action"] == "merge" and r2.get("verified"), f"list_notes_folder auto-approved and published: {r2['decision']['reasons']} {r2.get('error', '')[:300]}")
    r3 = s.submit(HELD, "someone", FLAG)
    check(r3["decision"]["action"] == "review", f"a stub 'flag' verdict is held for human review: {r3['decision']['reasons']}")
    names = sorted(e["name"] for e in s.index()["skills"])
    check(names == ["list_notes_folder", "repo_status"], f"index has exactly the two approved skills: {names}")
    by = {e["review"]["screened_by"] for e in s.index()["skills"]}
    check(all(b.endswith("(stub:ok)") and "{" not in b for b in by), f"review.screened_by names the model and the stub (BUGS.md #70): {by}")
    s.serve()
    s.prepare_home()
    s.start_agent()

    async with websockets.connect(s.agent_url, additional_headers=ws_auth.bearer(s.token), max_size=20 * 1024 * 1024) as ws:
        policy = {"approve": True, "remember": "always"}
        c = Client(ws, lambda card: (policy["approve"], policy["remember"]))

        print("# discover and install")
        listing = await c.control({"type": "registry_list", "refresh": True})
        check(listing["ok"] and listing["index_ok"] and sorted(x["name"] for x in listing["skills"]) == names
              and listing["key_id"] == entry["key_id"] and listing["checked_at"] and listing["imported_max"] == 20,
              "registry_list: the two skills, signed by the test key, checked_at, imported_max")
        rs = next(x for x in listing["skills"] if x["name"] == "repo_status")
        check(rs["has_run_command"] and rs["author"] == "octo-cat" and rs["review"]["verdict"] == "auto_approved"
              and rs["installed_version"] is None, "index entry: run_command, author, review")
        card = await c.control({"type": "registry_preview", "name": "repo_status", "content_hash": rs["content_hash"]})
        check(card["ok"] and [st["tool"] for st in card["steps"]] == ["get_current_datetime", "run_command"]
              and card["steps"][1]["run_command"] and card["steps"][1]["args"]["cmd"] == "git status --short"
              and card["model_description"].startswith("Shared skill by octo-cat")
              and card["author_description"] == REPO_STATUS["description"], "preview: every step with args, run_command flagged, both descriptions")
        inv = await c.control({"type": "inventory"})
        check("repo_status" not in {x["name"] for x in inv["skills"]}, "previewing installs nothing")
        result = await c.control({"type": "registry_install", "name": "repo_status", "content_hash": rs["content_hash"]})
        check(result["ok"] and result["version"] == 1, "registry_install installs v1")
        inv = {x["name"]: x for x in (await c.control({"type": "inventory"}))["skills"]}
        mine = inv.get("repo_status") or {}
        check(mine.get("origin") == "imported" and mine["registry"]["version"] == 1 and mine["registry"]["first_run_done"] is False
              and mine["description"].startswith("Shared skill by octo-cat"), "inventory: origin imported, record, local description")
        check(inv["client_notes"]["origin"] == "generated", "the learned skill stays origin generated")

        print("# first run asks every step; run_command asks on every run, never trusted")
        c.cards.clear()
        out = await c.run("repo_status")
        tools = [card["tool"] for card in c.cards]
        check(out["ok"] and tools == ["get_current_datetime", "run_command"], f"first run: every step asked ({tools})")
        check(all("shared skill repo_status" in card.get("reason", "") for card in c.cards), "each card says why (the shared-skill reason)")
        inv = {x["name"]: x for x in (await c.control({"type": "inventory"}))["skills"]}
        check(inv["repo_status"]["registry"]["first_run_done"] is True, "first_run_done after a clean run")
        c.cards.clear()
        out = await c.run("repo_status")
        check(out["ok"] and [card["tool"] for card in c.cards] == ["run_command"], "second run: only the run_command step asks")
        c.cards.clear()
        await c.run("repo_status")
        check([card["tool"] for card in c.cards] == ["run_command"], "third run: it still asks (the 'always' answers weren't kept)")
        trusted = (await c.control({"type": "inventory"}))["trusted"]
        check(not any(t.get("tool") == "run_command" for t in trusted), "nothing from the shared skill is in trust")
        policy["approve"] = False
        c.cards.clear()
        out = await c.run("repo_status")
        check(not out["ok"] and [card["tool"] for card in c.cards] == ["run_command"], "declining the command fails the run")
        policy["approve"] = True

        print("# an update is offered, not applied")
        r = s.submit(REPO_STATUS_V2, "octo-cat", APPROVE)
        check(r["decision"]["action"] == "merge" and r.get("verified"), "v2 published")
        listing = await c.control({"type": "registry_list", "refresh": True})
        rs2 = next(x for x in listing["skills"] if x["name"] == "repo_status")
        inst = next(x for x in listing["installed"] if x["name"] == "repo_status")
        check(rs2["version"] == 2 and rs2["installed_version"] == 1 and rs2["update_available"] and inst["update_available"]
              and inst["version"] == 1, "registry_list offers v2 over installed v1")
        inv = {x["name"]: x for x in (await c.control({"type": "inventory"}))["skills"]}
        check(inv["repo_status"]["registry"]["version"] == 1 and len(inv["repo_status"]["steps"]) == 2, "installed copy is still v1")

        print("# yank")
        y = s.yank(REPO_STATUS)
        check(y.get("yanked") and y.get("verified"), "v1 yanked and the index re-signed")
        listing = await c.control({"type": "registry_list", "refresh": True})
        inst = next(x for x in listing["installed"] if x["name"] == "repo_status")
        check(inst["withdrawn"] and inst["in_index"], "installed v1 shows withdrawn")
        inv = {x["name"]: x for x in (await c.control({"type": "inventory"}))["skills"]}
        check("repo_status" in inv, "withdrawn is not removed")
        bad = await c.control({"type": "registry_install", "name": "repo_status", "content_hash": REPO_STATUS["content_hash"]})
        check(not bad["ok"] and bad["error_code"] == "yanked", "a yanked version can't be installed (error_code yanked)")

        print("# tampered index")
        s.tamper(True)
        listing = await c.control({"type": "registry_list", "refresh": True})
        check(not listing["ok"] and not listing["index_ok"] and listing["error_code"] == "verification_failed"
              and listing["skills"] == [] and [x["name"] for x in listing["installed"]] == ["repo_status"],
              f"refused: {listing.get('error')}; installed still listed")
        bad = await c.control({"type": "registry_preview", "name": "list_notes_folder", "content_hash": NOTES_LIST["content_hash"]})
        check(not bad["ok"] and bad["error_code"] == "verification_failed", "nothing previews from a tampered index")
        bad = await c.control({"type": "registry_install", "name": "list_notes_folder", "content_hash": NOTES_LIST["content_hash"]})
        check(not bad["ok"] and bad["error_code"] == "verification_failed", "nothing installs from a tampered index")
        s.tamper(False)
        listing = await c.control({"type": "registry_list", "refresh": True})
        check(listing["ok"] and listing["index_ok"], "the untampered index verifies again")

        print("# share a learned skill")
        prev = await c.control({"type": "registry_share_preview", "skill": "client_notes"})
        check(prev["ok"] and prev["path"] == f"skills/client_notes/{prev['content_hash']}.json"
              and prev["repo"] == "AnuragK-O/leutheria-skills" and prev["too_long"] is False, "share preview: path, repo, too_long")
        check(str(s.home) not in prev["json"] and prev["definition"]["steps"][0]["args"]["path"] == "~/Documents/Clients"
              and any(ch["where"].endswith("steps[0].args.path") for ch in prev["changes"]), f"home path became ~, listed as a change: {prev['definition']['steps'][0]} {prev['changes']}")
        check(any(f["kind"] == "email" and f["value"] == "bob@example.com" for f in prev["flags"]), "the email is flagged")
        link = await c.control({"type": "registry_share", "skill": "client_notes"})
        check(link["ok"] and link["url"].startswith("https://github.com/AnuragK-O/leutheria-skills/new/main?filename=skills%2Fclient_notes%2F")
              and link["copy_to_clipboard"] is False and "value=" in link["url"], "registry_share: the registry repo's new-file URL, prefilled")
        bad = await c.control({"type": "registry_share_preview", "skill": "repo_status"})
        check(not bad["ok"] and bad["error_code"] == "not_shareable", "an imported skill can't be re-shared")

        print("# the shared file goes back through the pipeline")
        shared = s.tmp / "shared_client_notes.json"
        shared.write_text(prev["json"])
        r = s._pipeline("submit", "--skill", str(shared), "--author", "me", "--screen-script", str(s.screen_wrapper), stub=APPROVE)
        check(r["decision"]["action"] == "merge" and r.get("verified"),
              "the exported file passes the registry pipeline and is published")
        await ws.close()


async def fixture():
    s = Setup()
    try:
        s.keygen()
        for definition, author in ((REPO_STATUS, "octo-cat"), (NOTES_LIST, "mira-k")):
            r = s.submit(definition, author, APPROVE)
            if r["decision"]["action"] != "merge" or not r.get("verified"):
                raise SystemExit(f"fixture: {definition['name']} not published: {r}")
        s.serve()
        s.prepare_home()
        s.start_agent()
        print(json.dumps({"url": s.agent_url, "token": s.token, "port": s.agent_port, "registry": s.registry_url,
                          "repo_status_v1": REPO_STATUS["content_hash"]}), flush=True)
        loop = asyncio.get_running_loop()
        while True:
            line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
            if not line or line == "quit":
                break
            try:
                if line == "v2":
                    ok = s.submit(REPO_STATUS_V2, "octo-cat", APPROVE).get("verified")
                elif line == "yank":
                    ok = s.yank(REPO_STATUS).get("verified")
                elif line in ("tamper", "untamper"):
                    s.tamper(line == "tamper")
                    ok = True
                else:
                    raise ValueError("unknown command")
                print(f"ok {line}" if ok else f"error {line}: pipeline failed", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"error {line}: {e}", flush=True)
    finally:
        s.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", action="store_true")
    asyncio.run(fixture() if parser.parse_args().fixture else full_loop())
