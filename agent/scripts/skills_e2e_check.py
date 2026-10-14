"""End-to-end check: say something -> the model picks capabilities -> they run.

    cd agent
    ./.venv/bin/python scripts/skills_e2e_check.py              # real model, costs money
    ./.venv/bin/python scripts/skills_e2e_check.py --scripted   # no model: replays a fixed plan
    ./.venv/bin/python scripts/skills_e2e_check.py timers zip_then_reveal
    ./.venv/bin/python scripts/skills_e2e_check.py --list

Each scenario is one or more spoken requests driven through the real
agent_loop.run() and the real dispatch(), so skills run their real steps.
Follow-up requests in a scenario rely on the conversation carrying over
("zip that up too", "now open it"). A scenario passes on the EFFECT (files in
the sandbox, what the recorders saw) plus a spoken reply that is non-empty,
free of markdown and says what happened. Failure scenarios assert that a
refusal is reported as one, never as a success.

WITHOUT --scripted THIS CALLS THE REAL MODEL: roughly 60 requests, each
carrying the whole tool list (about 22,700 input tokens). --scripted swaps
the backend for one that replays each scenario's written plan, which proves
the sandbox, the stubs and the assertions for free but says nothing about
what the model would choose; a failure reply there is the tool's own error
text, so it still checks that the error says what went wrong.

Isolation. Someone is usually sitting at this Mac, and here a model decides
what gets called, so nothing is left to the model's good behaviour:

  home        HOME and files_common.HOME_DIR point at a temp dir rebuilt for
              every scenario, and the process chdirs there, so "~", a default
              and a relative path all land in the sandbox.
  paths       every tool function is wrapped: a path argument that resolves
              outside the sandbox is refused and fails the scenario.
  allowlist   a tool with no entry in POLICY below is refused, so a tool
              added later cannot reach the machine through this script.
  shell       run_command never reaches a shell. Only `git` (init, add,
              commit, status, log, diff, branch, pull, push) against a
              sandbox repository runs, as an argv list; `open -a` is
              recorded; anything else is declined at the confirmation.
  GUI         focus_app / type_text / press_keys are declined; describe_ui
              and list_running_apps return canned data.
  launchers   open_url, open_app, open_file, open_with, reveal_in_finder
              record what they would have opened and open nothing.
  sound       volume is a fake that models level and mute; media keys are
              recorded, never posted; speech and banners are recorded.
  clipboard   an in-memory fake; the real clipboard is never read or written.
  network     weather runs against a canned server on 127.0.0.1; fetch_url
              and Spotlight are stand-ins (Spotlight would otherwise send
              the names of the user's real files to the model).
  screen      take_screenshot writes a placeholder file; nothing is captured.
  trash       trash_file moves into a folder inside the sandbox.
  timers      real, with the clock moved forward so they fire in under a
              second. Nothing binds port 8765 or opens the microphone.

Prints one line per check and exits non-zero if any fail.
"""

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

AGENT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = AGENT_DIR.parent
sys.path.insert(0, str(AGENT_DIR))

REAL_HOME = Path.home()
TMP = Path(os.path.realpath(tempfile.mkdtemp(prefix="leutheria_e2e_check_")))
HOME = TMP / "home"
HOME.mkdir()
os.environ["HOME"] = str(HOME)
os.environ["LEUTHERIA_FILES_HOME"] = str(HOME)
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ["GIT_TERMINAL_PROMPT"] = "0"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
for _name in ("GIT_DIR", "GIT_WORK_TREE", "LEUTHERIA_WEATHER_URL", "LEUTHERIA_GEOCODE_URL"):
    os.environ.pop(_name, None)
os.chdir(HOME)

from agent.core import logging_util  # noqa: E402

logging_util.LOG_DIR = TMP / "logs"
logging_util.LOG_FILE = logging_util.LOG_DIR / "events.jsonl"

from agent.core import preferences, trust  # noqa: E402

preferences.PREFS_FILE = TMP / "preferences.json"
preferences._overrides.clear()
trust.TRUST_FILE = TMP / "trusted_commands.json"
trust._permanent_trust.clear()
trust._session_trust.clear()

from agent.core import agent_loop, dispatcher, session, timers, tts, weather  # noqa: E402
from agent.core.llm import LLMBackend, LLMTurn, ToolCall  # noqa: E402
from agent.skills import files_common  # noqa: E402
from agent.skills import registry as skills_registry  # noqa: E402
from agent.skills.registry import SKILLS  # noqa: E402
from agent.tools import get_volume as volume_tool  # noqa: E402
from agent.tools import media_control as media_tool  # noqa: E402
from agent.tools import open_file as open_file_tool  # noqa: E402
from agent.tools import open_with as open_with_tool  # noqa: E402
from agent.tools import reveal_in_finder as reveal_tool  # noqa: E402
from agent.tools.registry import TOOLS  # noqa: E402

for _name in [n for n, s in SKILLS.items() if s.get("origin") == "generated"]:
    SKILLS.pop(_name)
skills_registry.GENERATED_DIR = TMP / "generated"
files_common.HOME_DIR = str(HOME)
weather.HOME_FILE = TMP / "home_location.json"

passed = 0
failures = []


def check(condition, label):
    global passed
    if condition:
        passed += 1
        print(f"ok    {label}", flush=True)
    else:
        failures.append(label)
        print(f"FAIL  {label}", flush=True)


# --- recorders and stand-ins ---------------------------------------------------


class Rec:
    """Everything a scenario can assert on besides the sandbox's files."""

    urls, apps, launches, notifications, spoken, media = [], [], [], [], [], []
    commands, declined, asked, violations, gui, calls = [], [], [], [], [], []
    clipboard = ""

    @classmethod
    def reset(cls):
        for name in ("urls", "apps", "launches", "notifications", "spoken", "media",
                     "commands", "declined", "asked", "violations", "gui", "calls"):
            getattr(cls, name).clear()
        cls.clipboard = ""


class FakeVolume:
    """Stands in for osascript: models level and mute, and refuses any script
    other than the two exact forms the volume tools send."""

    SET = re.compile(r"set volume output volume (\d{1,3}) output muted (true|false)")
    level, muted = 44, False

    def __call__(self, script: str) -> tuple:
        if script == "get volume settings":
            return 0, (f"output volume:{self.level}, input volume:20, alert volume:100, "
                       f"output muted:{str(self.muted).lower()}\n"), ""
        match = self.SET.fullmatch(script)
        if not match:
            raise AssertionError(f"unexpected AppleScript: {script!r}")
        self.level, self.muted = int(match.group(1)), match.group(2) == "true"
        return 0, "", ""


fake_volume = FakeVolume()
volume_tool._osascript = fake_volume
media_tool._post_media_key = lambda key_code: Rec.media.append(key_code)
media_tool._can_post = lambda: True


def _fake_launch(argv, timeout=3.0):
    Rec.launches.append(list(argv))
    return 0, "", ""


def _fake_open_with_launch(argv):
    Rec.launches.append(list(argv))
    return subprocess.CompletedProcess(argv, 0, "", "")


open_file_tool._launch = _fake_launch
reveal_tool._launch = _fake_launch
open_with_tool._launch = _fake_open_with_launch


async def _fake_speak(websocket, text):
    Rec.spoken.append(text)


tts.speak = _fake_speak
dispatcher.speak = _fake_speak
tts.set_player(lambda audio, rate: None)


def in_sandbox(value) -> bool:
    try:
        resolved = Path(os.path.realpath(Path(str(value)).expanduser()))
    except (OSError, ValueError):
        return False
    return resolved == TMP or TMP in resolved.parents


def _stub_open_url(url: str) -> dict:
    Rec.urls.append(url)
    return {"ok": True, "message": f"opened {url}"}


def _stub_open_app(name: str) -> dict:
    Rec.apps.append(name)
    return {"ok": True, "message": f"opened {name}"}


def _stub_notify(title: str, message: str) -> dict:
    Rec.notifications.append((title, message))
    return {"ok": True, "message": "notification shown"}


def _stub_get_clipboard() -> dict:
    return {"ok": True, "text": Rec.clipboard}


def _stub_set_clipboard(text: str) -> dict:
    Rec.clipboard = text
    return {"ok": True, "message": "copied to clipboard"}


def _stub_screenshot(save_path: str = None) -> dict:
    path = Path(save_path).expanduser() if save_path else HOME / "Desktop" / f"screenshot-{int(time.time())}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return {"ok": False, "error": f"{path} already exists"}
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"placeholder, not a capture")
    return {"ok": True, "path": str(path)}


def _stub_trash(path: str) -> dict:
    target = Path(path).expanduser()
    if not target.exists():
        return {"ok": False, "error": f"{target} does not exist"}
    trash = TMP / "Trash"
    trash.mkdir(exist_ok=True)
    shutil.move(str(target), str(trash / f"{int(time.time() * 1000)}-{target.name}"))
    return {"ok": True, "message": f"moved {target} to Trash"}


def _stub_spotlight(query: str, folder: str = None, limit: int = 20) -> dict:
    root = Path(folder).expanduser() if folder else HOME
    hits = [str(p) for p in sorted(root.rglob("*"))
            if query.lower() in p.name.lower() and not any(part.startswith(".") for part in p.relative_to(root).parts)]
    return {"ok": True, "query": query, "count": len(hits[:limit]), "results": hits[:limit]}


PAGE_TEXT = (
    "The Long Read. Sourdough starter needs feeding once a day at room temperature. "
    "Use equal weights of flour and water. A healthy starter doubles in about six hours."
)


def _stub_fetch_url(url: str, max_chars: int = 8000) -> dict:
    if not re.match(r"https?://", str(url)):
        return {"ok": False, "error": "only http and https addresses can be fetched"}
    return {"ok": True, "url": url, "final_url": url, "status": 200, "title": "The Long Read",
            "text": PAGE_TEXT[:max_chars], "truncated": False, "content_type": "text/html"}


def _stub_gui(tool):
    def fn(**args):
        Rec.gui.append((tool, args))
        return {"ok": False, "error": f"{tool} is not available in this test run"}
    return fn


GIT_ALLOWED = {"init", "add", "commit", "status", "log", "diff", "branch", "pull", "push"}
# Options are an allowlist too: --upload-pack, --output and friends would let
# a git command run a program or write a file of its choosing.
GIT_FLAGS = {"-A", "-a", "-m", "-am", "-q", "--quiet", "--all", "--porcelain", "--short", "--ff-only",
             "--oneline", "--stat", "--"}
GIT_FLAG_PATTERN = re.compile(r"-\d+|-n|--(format|pretty)=[%\w :,.-]*")


def plan_command(cmd: str):
    """(segments, None) when every part of a shell command is one this check
    will carry out, else (None, reason). Nothing is run here."""
    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        return None, f"unparseable command: {e}"
    if any("$" in t or "`" in t for t in tokens):
        return None, "shell substitution is not run in this test"
    segments, current = [], []
    for token in tokens:
        if token == "&&":
            segments.append(current)
            current = []
        else:
            current.append(token)
    segments.append(current)
    cwd = None
    for seg in segments:
        if len(seg) == 2 and seg[0] == "cd":
            if not in_sandbox(seg[1]):
                return None, "cd outside the sandbox"
            cwd = seg[1]
        elif seg[:1] == ["git"]:
            rest = seg[1:]
            where = cwd
            if rest[:1] == ["-C"] and len(rest) >= 3:
                where, rest = rest[1], rest[2:]
            if where is None or not in_sandbox(where):
                return None, "git outside a sandbox repository"
            if not rest or rest[0] not in GIT_ALLOWED:
                return None, f"git {rest[0] if rest else ''} is not run in this test"
            message_next = False
            for token in rest[1:]:
                if not message_next and token.startswith("-") and token not in GIT_FLAGS \
                        and not GIT_FLAG_PATTERN.fullmatch(token):
                    return None, f"git option {token} is not run in this test"
                message_next = token in ("-m", "-am")
        elif seg[:2] == ["open", "-a"] and len(seg) == 4:
            if not in_sandbox(seg[3]):
                return None, "open outside the sandbox"
        else:
            return None, f"{seg[0] if seg else 'empty command'} is not run in this test"
    return segments, None


def _guarded_run_command(cmd: str) -> dict:
    segments, reason = plan_command(cmd)
    if segments is None:
        Rec.violations.append(f"run_command reached the tool with a refused command: {cmd!r} ({reason})")
        return {"ok": False, "stdout": "", "stderr": reason}
    Rec.commands.append(cmd)
    cwd, out = None, []
    for seg in segments:
        if seg[0] == "cd":
            cwd = str(Path(seg[1]).expanduser())
        elif seg[0] == "open":
            Rec.launches.append(seg)
        else:
            # No shell, so "~" is expanded here; -C is the only place a path is accepted.
            argv = [str(Path(t).expanduser()) if i == 2 and seg[1] == "-C" else t for i, t in enumerate(seg)]
            done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)
            out.append(done.stdout.strip())
            if done.returncode != 0:
                return {"ok": False, "stdout": "\n".join(out), "stderr": done.stderr.strip()}
    return {"ok": True, "stdout": "\n".join(o for o in out if o), "stderr": ""}


CANNED = {
    "get_battery_status": {"ok": True, "has_battery": True, "percent": 80, "state": "charging"},
    "get_disk_space": {"ok": True, "total_gb": 500.0, "used_gb": 300.0, "free_gb": 200.0},
    "get_system_info": {"ok": True, "cpu_load_percent": 12, "memory": {"used_percent": 50, "pressure": "normal"},
                        "uptime_seconds": 3600, "uptime": "1 hour", "message": "Load is light and memory is fine."},
    "get_network_status": {"ok": True, "connected": True, "internet": True, "kind": "Wi-Fi",
                           "message": "Connected over Wi-Fi."},
    "list_running_apps": {"ok": True, "apps": ["Finder", "Safari", "Music"]},
    "describe_ui": {"ok": False, "error": "describe_ui is not available in this test run"},
}

# Every tool must be named here. "real" runs the shipped function behind the
# path guard (its outside reach already replaced above, where it has one).
REAL = {
    "create_folder", "list_files", "read_file", "append_text", "move_file", "copy_file", "find_files",
    "file_info", "folder_size", "zip_folder", "extract_archive", "find_duplicates", "write_new_file",
    "search_file_contents", "git_status", "get_current_datetime", "set_timer", "list_timers",
    "cancel_timer", "get_volume", "set_volume", "media_control", "get_weather", "set_home_location",
    "open_file", "reveal_in_finder", "open_with",
}
STUBS = {
    "open_url": _stub_open_url, "open_app": _stub_open_app, "show_notification": _stub_notify,
    "get_clipboard": _stub_get_clipboard, "set_clipboard": _stub_set_clipboard,
    "take_screenshot": _stub_screenshot, "trash_file": _stub_trash, "spotlight_search": _stub_spotlight,
    "fetch_url": _stub_fetch_url, "run_command": _guarded_run_command,
    "focus_app": _stub_gui("focus_app"), "type_text": _stub_gui("type_text"), "press_keys": _stub_gui("press_keys"),
    **{name: (lambda n: lambda **_: dict(CANNED[n]))(name) for name in CANNED},
}
PATH_KEYS = {"path", "source", "destination", "save_path", "archive", "folder"}


def _guard(name, fn):
    def guarded(**args):
        Rec.calls.append(name)
        for key, value in args.items():
            if key in PATH_KEYS and isinstance(value, str) and value.strip() and not in_sandbox(value):
                Rec.violations.append(f"{name}({key}={value!r}) points outside the sandbox")
                return {"ok": False, "error": f"{value} is outside the folder this test may touch"}
        return fn(**args)
    return guarded


def _refuse(name):
    def refused(**args):
        Rec.violations.append(f"{name} has no policy in skills_e2e_check.py and was refused")
        return {"ok": False, "error": f"{name} is not available in this test run"}
    return refused


for _name, _tool in TOOLS.items():
    if _name in REAL:
        _tool["fn"] = _guard(_name, _tool["fn"])
    elif _name in STUBS:
        _tool["fn"] = _guard(_name, STUBS[_name])
    else:
        _tool["fn"] = _refuse(_name)


class FakeSocket:
    """The UI: answers each confirmation the way a careful user would here.
    A shell command is approved only if this check is able to carry it out
    safely; driving an app is declined."""

    def __init__(self, pending):
        self.pending = pending

    async def send(self, raw):
        message = json.loads(raw)
        if message.get("type") != "confirmation_required":
            return
        tool, args = message["tool"], message["args"]
        approve = tool == "run_command" and plan_command(args.get("cmd", ""))[0] is not None
        Rec.asked.append((tool, args, approve))
        if not approve:
            Rec.declined.append((tool, args))
        self.pending[message["id"]].set_result({"approved": approve, "remember": None})


# --- canned Open-Meteo ---------------------------------------------------------


class OpenMeteo(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        OpenMeteo.seen.append((url.path, query))
        if url.path == "/v1/search":
            name = query.get("name", "Paris")
            body = {"results": [{"name": name.title(), "admin1": "Ile-de-France", "country": "France",
                                 "country_code": "FR", "latitude": 48.85, "longitude": 2.35}]}
        else:
            days = int(query.get("forecast_days", 1))
            imperial = query.get("temperature_unit") == "fahrenheit"
            body = {
                "current": {"temperature_2m": 72 if imperial else 22, "apparent_temperature": 75 if imperial else 24,
                            "relative_humidity_2m": 40, "weather_code": 2, "wind_speed_10m": 8.3},
                "daily": {
                    "time": [f"2026-10-{13 + i}" for i in range(days)],
                    "weather_code": [2, 61, 0, 95, 3, 71, 1][:days],
                    "temperature_2m_max": ([80, 70, 75, 77, 78, 60, 65] if imperial else [27, 21, 24, 25, 26, 16, 18])[:days],
                    "temperature_2m_min": ([61, 55, 58, 60, 61, 40, 45] if imperial else [16, 13, 14, 16, 16, 4, 7])[:days],
                    "precipitation_probability_max": [20, 70, 0, 90, 10, 30, 5][:days],
                },
            }
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


_http = ThreadingHTTPServer(("127.0.0.1", 0), OpenMeteo)
_http.daemon_threads = True
threading.Thread(target=_http.serve_forever, daemon=True).start()
weather.FORECAST_URL = f"http://127.0.0.1:{_http.server_address[1]}/v1/forecast"
weather.GEOCODE_URL = f"http://127.0.0.1:{_http.server_address[1]}/v1/search"

# --- the sandbox home ----------------------------------------------------------

DAY = 86400


def put(relative, content="x", age_days=None) -> Path:
    path = HOME / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    if age_days is not None:
        stamp = time.time() - age_days * DAY
        os.utime(path, (stamp, stamp))
    return path


def git(repo: Path, *argv) -> str:
    done = subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True, timeout=30)
    return done.stdout.strip()


def build_home():
    for child in HOME.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    (HOME / ".gitconfig").write_text(
        "[user]\n\tname = E2E Check\n\temail = e2e-check@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n"
    )
    for name in ("Desktop", "Downloads", "Documents", "Pictures", "Projects", "Movies", "Music"):
        (HOME / name).mkdir()
    put("Desktop/holiday.jpg", b"\xff\xd8\xff" + b"j" * 2000, age_days=3)
    put("Desktop/todo.txt", "call the dentist\n", age_days=2)
    put("Desktop/song.mp3", b"ID3" + b"m" * 3000, age_days=9)
    put("Desktop/Screenshot 2026-10-12 at 09.14.03.png", b"\x89PNG" + b"s" * 1500, age_days=1)
    (HOME / "Desktop" / "Keep Me").mkdir()
    put("Desktop/Keep Me/inside.txt", "stays put\n")
    put("Downloads/old-manual.pdf", b"%PDF-1.4 " + b"o" * 4000, age_days=90)
    put("Downloads/invoice-october.pdf", b"%PDF-1.4 " + b"i" * 4000, age_days=4)
    with zipfile.ZipFile(HOME / "Downloads" / "fonts.zip", "w") as z:
        z.writestr("fonts/Inter.txt", "pretend this is a font\n")
        z.writestr("fonts/README.txt", "two files\n")
    put("Documents/taxes/2025-return.txt", "refund expected\n", age_days=30)
    put("Documents/taxes/receipts.csv", "date,amount\n2025-03-01,120\n", age_days=30)
    put("Documents/resume.pdf", b"%PDF-1.4 " + b"r" * 3000, age_days=20)
    put("Documents/plan.md", "ORIGINAL PLAN: do not lose this line\n", age_days=5)
    script = put("Documents/run-me.command", "#!/bin/sh\necho this must never run\n", age_days=5)
    script.chmod(0o755)
    repo = HOME / "Projects" / "atlas"
    repo.mkdir()
    put("Projects/atlas/app.py", "print('v1')\n")
    put("Projects/atlas/README.md", "# atlas\n\nA small mapping tool.\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first commit")
    put("Projects/atlas/app.py", "print('v2')\n")
    put("Projects/atlas/notes.md", "uncommitted\n")


def tree() -> set:
    return {str(p.relative_to(HOME)) for p in HOME.rglob("*") if ".git" not in p.relative_to(HOME).parts}


def files_under(relative) -> list:
    root = HOME / relative
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []


# --- backends --------------------------------------------------------------------


class ScriptedBackend(LLMBackend):
    """Replays a written plan instead of asking a model. Each item is a list
    of (tool, args) for a tool-calling turn, the final text, or a function of
    the results so far that returns either (so a follow-up can name what an
    earlier step made, and a failure reply can be the tool's own words)."""

    def __init__(self):
        self.plan, self.results = [], []

    def load(self, plan):
        self.plan, self.results = list(plan), []

    def generate(self, messages, tools, system=None):
        if messages and isinstance(messages[-1]["content"], list):
            self.results += [json.loads(block["content"]) for block in messages[-1]["content"]]
        if not self.plan:
            return LLMTurn(content=[], tool_calls=[], text="(the scripted plan ran out)")
        item = self.plan.pop(0)
        if callable(item):
            item = item(self.results)
        if isinstance(item, list):
            calls = [ToolCall(id=f"call_{len(self.results)}_{i}", name=name, args=args)
                     for i, (name, args) in enumerate(item)]
            content = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in calls]
            return LLMTurn(content=content, tool_calls=calls, text="")
        return LLMTurn(content=[{"type": "text", "text": item}], tool_calls=[], text=item)


def said(results) -> str:
    """A scripted failure reply: whatever the last tool reported, verbatim."""
    last = results[-1] if results else {}
    return str(last.get("error") or last.get("message") or last.get("stderr") or last)


def load_key() -> None:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        for parent in REPO_ROOT.parents:  # a git worktree: the key is in the enclosing checkout
            if (parent / ".env").exists() and (parent / "agent").is_dir():
                load_dotenv(parent / ".env")
                break


# --- reply checks ----------------------------------------------------------------

MARKDOWN = re.compile(r"\*\*|`|^\s{0,3}#{1,6}\s|^\s*[-*]\s+\S|\[[^\]]+\]\([^)]+\)|^\s*\d+\.\s+\S+.*\n\s*\d+\.\s", re.M)
SUCCESS_CLAIM = re.compile(r"\b(done|created|saved|opened|wrote|written|organi[sz]ed|sorted|all set)\b", re.I)


def reply_ok(name, reply, mentions=None, honest_failure=None):
    """The reply is something a person could be told aloud."""
    text = reply or ""
    check(bool(text.strip()) and "gave up after too many" not in text, f"{name}: there is a spoken reply")
    check(not MARKDOWN.search(text), f"{name}: the reply has no markdown ({text[:70]!r})")
    if mentions:
        check(re.search(mentions, text, re.I) is not None, f"{name}: the reply says what happened ({text[:90]!r})")
    if honest_failure:
        check(re.search(honest_failure, text, re.I) is not None,
              f"{name}: the reply admits it did not happen ({text[:110]!r})")


# --- scenarios -------------------------------------------------------------------
#
# Each returns (turns, verify). A turn is (what is said, scripted plan).
# verify(replies, traces) makes the checks.


def tools_used(traces) -> list:
    return [step["tool"] for trace in traces for step in trace]


def s_zip_then_reveal():
    turns = [
        ("zip up my taxes folder in Documents",
         [[("archive_folder", {"path": "~/Documents/taxes"})], "Zipped taxes into taxes.zip, next to the folder in Documents."]),
        ("now show me where it is",
         [[("reveal_in_finder", {"path": "~/Documents/taxes.zip"})], "It's selected in Finder, in Documents."]),
    ]

    def verify(replies, traces):
        zips = sorted((HOME / "Documents").glob("*.zip"))
        check(len(zips) == 1 and zipfile.is_zipfile(zips[0]), "zip_then_reveal: one zip was made in Documents")
        if zips:
            names = zipfile.ZipFile(zips[0]).namelist()
            check(any(n.endswith("receipts.csv") for n in names), "zip_then_reveal: the zip holds the folder's files")
        check(files_under("Documents/taxes") == ["2025-return.txt", "receipts.csv"], "zip_then_reveal: the folder is untouched")
        reveals = [a for a in Rec.launches if a[:2] == ["open", "-R"]]
        check(bool(zips) and any(a[-1] == str(zips[0]) for a in reveals),
              "zip_then_reveal: the follow-up 'show me where it is' revealed that zip, not something else")
        reply_ok("zip_then_reveal", replies[0], mentions=r"zip")
        reply_ok("zip_then_reveal (follow-up)", replies[1], mentions=r"finder|documents|selected|showing|here")
    return turns, verify


def s_backup_then_zip():
    def zip_the_backup(results):
        names = [e if isinstance(e, str) else e.get("name", "") for e in results[-1].get("files", [])]
        backup = next(n for n in names if "taxes" in n and n != "taxes")
        return [("archive_folder", {"path": f"~/Documents/{backup}"})]

    turns = [
        ("back up my taxes folder in Documents before I touch it",
         [[("backup_folder", {"path": "~/Documents/taxes"})], "Backed up taxes to a timestamped copy next to it."]),
        ("zip that up too",
         [[("list_files", {"path": "~/Documents"})], zip_the_backup, "Zipped the backup as well."]),
    ]

    def verify(replies, traces):
        copies = [p for p in (HOME / "Documents").iterdir() if p.is_dir() and p.name != "taxes" and "taxes" in p.name]
        check(len(copies) == 1 and files_under(f"Documents/{copies[0].name}") == ["2025-return.txt", "receipts.csv"] if copies else False,
              "backup_then_zip: a second copy of the folder exists alongside, with both files")
        zips = list((HOME / "Documents").glob("*.zip"))
        check(len(zips) == 1, f"backup_then_zip: 'zip that up too' made exactly one zip ({[z.name for z in zips]})")
        check("archive_folder" in tools_used(traces[1:]) or "zip_folder" in tools_used(traces[1:]),
              "backup_then_zip: the follow-up zipped rather than backing up again")
        check(files_under("Documents/taxes") == ["2025-return.txt", "receipts.csv"], "backup_then_zip: the original is untouched")
        reply_ok("backup_then_zip", replies[0], mentions=r"back|cop")
        reply_ok("backup_then_zip (follow-up)", replies[1], mentions=r"zip")
    return turns, verify


def s_clean_desktop():
    turns = [("clean up my desktop", [[("clean_desktop", {})], "Moved four files into Desktop Cleanup, grouped by type."])]

    def verify(replies, traces):
        desktop = HOME / "Desktop"
        loose = [p.name for p in desktop.iterdir() if p.is_file() and not p.name.startswith(".")]
        check(loose == [], f"clean_desktop: no loose files are left on the Desktop ({loose})")
        moved = files_under("Desktop/Desktop Cleanup")
        check(sorted(Path(m).name for m in moved) == ["Screenshot 2026-10-12 at 09.14.03.png", "holiday.jpg", "song.mp3", "todo.txt"],
              f"clean_desktop: all four files are in Desktop Cleanup, none lost ({moved})")
        check((desktop / "Keep Me" / "inside.txt").exists(), "clean_desktop: a folder on the Desktop stayed where it was")
        check((TMP / "Trash").exists() is False, "clean_desktop: nothing was trashed")
        reply_ok("clean_desktop", replies[0], mentions=r"desktop|moved|clean")
    return turns, verify


def s_unzip_latest():
    turns = [("unzip the thing I just downloaded",
              [[("extract_latest_download", {})], "Unpacked fonts.zip into a new fonts folder in Downloads."])]

    def verify(replies, traces):
        found = [p for p in (HOME / "Downloads").rglob("Inter.txt")]
        check(len(found) == 1, "unzip_latest: the newest archive in Downloads was unpacked")
        check((HOME / "Downloads" / "fonts.zip").exists(), "unzip_latest: the archive itself was kept")
        reply_ok("unzip_latest", replies[0], mentions=r"fonts|unpack|unzip|extract")
    return turns, verify


def s_folder_and_file():
    turns = [("make a folder called Trip in Documents and put a file called packing.txt in it that says socks and charger",
              [[("create_folder", {"path": "~/Documents/Trip"})],
               [("write_new_file", {"path": "~/Documents/Trip/packing.txt", "content": "socks and charger\n"})],
               "Made the Trip folder in Documents with packing.txt inside."])]

    def verify(replies, traces):
        target = HOME / "Documents" / "Trip" / "packing.txt"
        check(target.is_file(), "folder_and_file: Documents/Trip/packing.txt exists")
        text = target.read_text().lower() if target.is_file() else ""
        check("socks" in text and "charger" in text, f"folder_and_file: it says what was dictated ({text!r})")
        reply_ok("folder_and_file", replies[0], mentions=r"trip|packing")
    return turns, verify


def s_refuse_existing_file():
    turns = [("write a file called plan.md in my Documents folder that says buy milk",
              [[("write_new_file", {"path": "~/Documents/plan.md", "content": "buy milk\n"})], said])]

    def verify(replies, traces):
        check((HOME / "Documents" / "plan.md").read_text() == "ORIGINAL PLAN: do not lose this line\n",
              "refuse_existing_file: the existing plan.md was not replaced or added to")
        others = [p.name for p in (HOME / "Documents").iterdir() if p.is_file() and "milk" in p.read_text(errors="ignore")]
        reply_ok("refuse_existing_file", replies[0])
        if others:
            check(any(o.lower() in replies[0].lower() or Path(o).stem.lower() in replies[0].lower() for o in others),
                  f"refuse_existing_file: it wrote {others} instead and says so by name")
        else:
            check(re.search(r"already|exist|there'?s (already )?a", replies[0], re.I) is not None,
                  f"refuse_existing_file: the reply says the file is already there ({replies[0][:110]!r})")
    return turns, verify


def s_refuse_missing_folder():
    turns = [("organize my Receipts folder in Documents",
              [[("organize_folder", {"path": "~/Documents/Receipts"})], said])]

    def verify(replies, traces):
        check(not (HOME / "Documents" / "Receipts").exists(), "refuse_missing_folder: no Receipts folder was conjured up")
        reply_ok("refuse_missing_folder", replies[0],
                 honest_failure=r"(n't|not|no |unable|could ?n.t).{0,60}(exist|find|found|there|folder)|does not exist|no such")
        moves = [t for t in tools_used(traces) if t == "move_file"]
        check(moves == [], "refuse_missing_folder: nothing was moved")
    return turns, verify


def s_refuse_open_script():
    turns = [("open run-me.command in my Documents folder",
              [[("open_file", {"path": "~/Documents/run-me.command"})], said])]

    def verify(replies, traces):
        opened = [a for a in Rec.launches if "run-me.command" in " ".join(a) and a[:2] != ["open", "-R"]]
        check(opened == [], f"refuse_open_script: the script was never handed to a launcher ({opened})")
        check(Rec.commands == [], f"refuse_open_script: no shell command ran ({Rec.commands})")
        reply_ok("refuse_open_script", replies[0],
                 honest_failure=r"can'?t|cannot|won'?t|refus|not able|unable|didn'?t|declin|would run|script|not open")
        check(not re.search(r"\b(opened it|it'?s open|is open now|running now|ran it)\b", replies[0], re.I),
              "refuse_open_script: the reply does not claim it opened or ran")
    return turns, verify


def s_timers():
    turns = [
        ("set a timer for ten minutes",
         [[("quick_timer", {"duration": "10 minutes"})], "Ten minute timer started."]),
        ("and another one for twenty minutes, call it laundry",
         [[("quick_timer", {"duration": "20 minutes", "label": "laundry"})], "Laundry timer set for twenty minutes."]),
        ("cancel the ten minute one",
         [[("list_timers", {})],
          lambda results: [("cancel_timer", {"timer_id": next(t["timer_id"] for t in results[-1]["timers"] if not t["label"])})],
          "Cancelled the ten minute timer. Laundry is still running."]),
    ]

    def verify(replies, traces):
        left = timers.active()
        check(len(left) == 1 and "laundry" in left[0]["label"].lower() and 1100 < left[0]["remaining_seconds"] <= 1200,
              f"timers: only the twenty-minute laundry timer is left ({[(t['label'], int(t['remaining_seconds'])) for t in left]})")
        check(any("timer" in (title + message).lower() for title, message in Rec.notifications),
              "timers: setting a timer put a banner on screen (through the recorder)")
        for i, reply in enumerate(replies):
            reply_ok(f"timers (turn {i + 1})", reply, mentions=r"timer|laundry|cancel|minute")
    return turns, verify


async def after_timers():
    """Move the clock past the laundry timer and see it announce itself."""
    Rec.spoken.clear()
    Rec.notifications.clear()
    offset["s"] = 1300.0
    for _ in range(60):
        if Rec.spoken:
            break
        await asyncio.sleep(0.05)
    check(len(Rec.spoken) == 1 and timers.active() == [], f"timers: the laundry timer fired once when its time came ({Rec.spoken})")
    check(len(Rec.notifications) == 1, "timers: and showed its banner through show_notification")


def s_volume():
    turns = [
        ("turn the volume down a bit", [[("set_volume", {"change": -15})], "Volume is down to 29."]),
        ("actually just mute it", [[("set_volume", {"muted": True})], "Muted."]),
    ]

    def verify(replies, traces):
        check(fake_volume.muted is True and fake_volume.level < 44,
              f"volume: lowered, then muted on the follow-up (level {fake_volume.level}, muted {fake_volume.muted})")
        check("focus_mode" not in tools_used(traces) and timers.active() == [],
              "volume: 'mute it' did not start focus mode or a timer")
        reply_ok("volume", replies[0], mentions=r"volume|down|lower|quiet|\d")
        reply_ok("volume (follow-up)", replies[1], mentions=r"mute|silen|quiet|off")
    return turns, verify


def s_weather():
    turns = [("what's the weather in Paris right now",
              [[("get_weather", {"place": "Paris", "units": "metric"})],
               lambda results: results[-1].get("message") or "It's 22 degrees and partly cloudy in Paris."])]

    def verify(replies, traces):
        check(any(q.get("name", "").lower().startswith("paris") for path, q in OpenMeteo.seen if path == "/v1/search"),
              "weather: Paris was looked up (on the canned server)")
        check(not weather.HOME_FILE.exists(), "weather: asking about a city did not save it as home")
        reply_ok("weather", replies[0], mentions=r"\b(22|24|72|75|27|80)\b")
    return turns, verify


def s_meeting():
    turns = [("I'm heading into a meeting in the conference room for half an hour, taking the laptop",
              [[("meeting_prep", {"kind": "in_person", "minutes": 30})],
               "Sound is muted and I'll tell you when the half hour is up. Battery is at 80 percent."])]

    def verify(replies, traces):
        check(fake_volume.muted is True, "meeting: the Mac was muted for an in-person meeting")
        left = timers.active()
        check(len(left) == 1 and 1700 < left[0]["remaining_seconds"] <= 1800,
              f"meeting: a thirty-minute timer is running ({[int(t['remaining_seconds']) for t in left]})")
        check(Rec.apps == [], "meeting: no app was opened that was not asked for")
        reply_ok("meeting", replies[0], mentions=r"mute|silen|quiet|sound")
    return turns, verify


def s_pause_music():
    turns = [("pause the music", [[("media_control", {"action": "play_pause"})], "Pressed play pause."])]

    def verify(replies, traces):
        check(Rec.media == [16], f"pause_music: play/pause was pressed exactly once ({Rec.media})")
        check(fake_volume.level == 44 and not fake_volume.muted, "pause_music: the volume was left alone")
        reply_ok("pause_music", replies[0], mentions=r"paus|play")
    return turns, verify


def s_python_project_then_open():
    turns = [
        ("make me a python project called starmap",
         [[("new_python_project", {"name": "starmap"})], "Created the starmap Python project in Projects."]),
        ("now open it in VS Code",
         [[("open_workspace", {"path": "~/Projects/starmap"})], "Starmap is open in Visual Studio Code."]),
    ]

    def verify(replies, traces):
        root = HOME / "Projects" / "starmap"
        check((root / "main.py").is_file() and (root / "README.md").is_file(), "python_project: starmap has main.py and a README")
        code = [a for a in Rec.launches if a[:2] == ["open", "-a"] and "code" in a[2].lower()]
        check(any(a[-1] == str(root) for a in code),
              f"python_project: 'now open it' opened that folder in VS Code ({Rec.launches})")
        check(len(list((HOME / "Projects").iterdir())) == 2, "python_project: no second project was created by the follow-up")
        reply_ok("python_project", replies[0], mentions=r"starmap")
        reply_ok("python_project (follow-up)", replies[1], mentions=r"open|code")
    return turns, verify


def s_git_commit():
    turns = [("commit everything in my atlas project in Projects with the message fix login",
              [[("git_snapshot", {"path": "~/Projects/atlas", "message": "fix login"})], "Committed two files to main as fix login."])]

    def verify(replies, traces):
        repo = HOME / "Projects" / "atlas"
        check(git(repo, "log", "-1", "--format=%s") == "fix login", "git_commit: the newest commit is 'fix login'")
        check(git(repo, "status", "--porcelain") == "", "git_commit: nothing is left uncommitted")
        check(any(tool == "run_command" and ok for tool, _, ok in Rec.asked), "git_commit: the commit command was put to the user first")
        reply_ok("git_commit", replies[0], mentions=r"commit")
    return turns, verify


def s_clipboard_note():
    def setup():
        Rec.clipboard = "call the plumber on 555-0100"
    turns = [("save what I copied to my notes file",
              [[("clipboard_to_note", {})], "Saved what you copied to your clipboard notes file. It isn't open on screen."])]

    def verify(replies, traces):
        notes = [p for p in (HOME / "Notes").rglob("*") if p.is_file()] if (HOME / "Notes").exists() else []
        check(any("555-0100" in p.read_text() for p in notes), "clipboard_note: the copied text is in a file under Notes")
        check(Rec.clipboard == "call the plumber on 555-0100", "clipboard_note: the clipboard itself was not changed")
        reply_ok("clipboard_note", replies[0], mentions=r"sav|note|clipboard|copied")
    return turns, verify, setup


def s_web_search():
    turns = [("search for best ramen in Austin",
              [[("web_search", {"query": "best ramen in Austin"})], "Search results for best ramen in Austin are open in your browser."])]

    def verify(replies, traces):
        check(len(Rec.urls) == 1 and "ramen" in Rec.urls[0].lower() and Rec.urls[0].startswith("https://"),
              f"web_search: one results page was opened, for ramen ({Rec.urls})")
        reply_ok("web_search", replies[0], mentions=r"search|result|browser|open|ramen")
        check(not re.search(r"\b(the best ramen (is|are)|top pick|I recommend)\b", replies[0], re.I),
              "web_search: the reply does not pretend to have read the results")
    return turns, verify


def s_reading_list_then_save():
    turns = [
        ("add https://example.com/long-read to my reading list",
         [[("read_later", {"url": "https://example.com/long-read"})], "Added it to your reading list."]),
        ("actually keep the whole page too",
         [[("save_page_notes", {"url": "https://example.com/long-read"})], "Saved the page's text as The Long Read in your Notes pages folder."]),
    ]

    def verify(replies, traces):
        notes = [p for p in (HOME / "Notes").rglob("*") if p.is_file()] if (HOME / "Notes").exists() else []
        check(any("example.com/long-read" in p.read_text() and "Sourdough" not in p.read_text() for p in notes),
              "reading_list: the link is in the reading list file")
        check(any("Sourdough starter" in p.read_text() for p in notes),
              "reading_list: the follow-up saved the same page's text, without being given the address again")
        check(Rec.urls == [], "reading_list: nothing was opened in the browser")
        reply_ok("reading_list", replies[0], mentions=r"reading list|added|saved")
        reply_ok("reading_list (follow-up)", replies[1], mentions=r"sav|page|kept|text")
    return turns, verify


SCENARIOS = [
    ("zip_then_reveal", "files", s_zip_then_reveal),
    ("backup_then_zip", "files", s_backup_then_zip),
    ("clean_desktop", "files", s_clean_desktop),
    ("unzip_latest", "files", s_unzip_latest),
    ("folder_and_file", "files", s_folder_and_file),
    ("refuse_existing_file", "files", s_refuse_existing_file),
    ("refuse_missing_folder", "files", s_refuse_missing_folder),
    ("refuse_open_script", "system", s_refuse_open_script),
    ("timers", "system", s_timers),
    ("volume", "system", s_volume),
    ("weather", "system", s_weather),
    ("meeting", "system", s_meeting),
    ("pause_music", "system", s_pause_music),
    ("python_project_then_open", "dev", s_python_project_then_open),
    ("git_commit", "dev", s_git_commit),
    ("clipboard_note", "dev", s_clipboard_note),
    ("web_search", "dev", s_web_search),
    ("reading_list_then_save", "dev", s_reading_list_then_save),
]
AFTER = {"timers": after_timers}

offset = {"s": 0.0}
timers._clock = lambda: time.time() + offset["s"]
timers.POLL_S = 0.05


async def run_scenario(name, build, backend, scripted, verbose):
    build_home()
    Rec.reset()
    OpenMeteo.seen.clear()
    session.clear_history()
    session.clear_focus()
    timers.reset()
    offset["s"] = 0.0
    fake_volume.level, fake_volume.muted = 44, False
    if weather.HOME_FILE.exists():
        weather.HOME_FILE.unlink()
    built = build()
    turns, verify = built[0], built[1]
    if len(built) > 2:
        built[2]()
    pending = {}
    socket = FakeSocket(pending)
    replies, traces = [], []
    for say, plan in turns:
        if scripted:
            backend.load(plan)
        result = await agent_loop.run(backend, say, socket, pending)
        replies.append(result.get("text", ""))
        traces.append(result.get("trace", []))
        if verbose:
            print(f"      > {say}")
            for step in result.get("trace", []):
                outcome = "ok" if isinstance(step["result"], dict) and step["result"].get("ok") else "FAILED"
                print(f"        {step['tool']}({json.dumps(step['args'], ensure_ascii=False)[:90]}) {outcome}")
            print(f"      < {result.get('text', '')[:200]}")
    verify(replies, traces)
    check(Rec.violations == [], f"{name}: nothing tried to leave the sandbox ({Rec.violations[:2]})")
    if name in AFTER:
        await AFTER[name]()
    timers.reset()


async def sandbox_holds():
    """Before any model is asked: the walls this script relies on, tried
    directly. Costs nothing and runs every time."""
    build_home()
    Rec.reset()
    pending = {}
    socket = FakeSocket(pending)
    outside = REAL_HOME / "Desktop" / "leutheria-e2e-must-not-exist.txt"

    async def call(tool, **args):
        return await dispatcher.dispatch(tool, args, socket, pending)

    result = await call("write_new_file", path=str(outside), content="x")
    check(result["ok"] is False and not outside.exists(), "sandbox: a write to the real Desktop is refused")
    result = await call("move_file", source="~/Documents/plan.md", destination=str(outside))
    check(result["ok"] is False and (HOME / "Documents" / "plan.md").exists(), "sandbox: a move out of the sandbox is refused")
    result = await call("list_files", path=str(REAL_HOME / "Documents"))
    check(result["ok"] is False, "sandbox: even listing a real folder is refused")
    result = await call("read_file", path="../../../../../../etc/hosts")
    check(result["ok"] is False, "sandbox: a relative path that climbs out is refused")
    # Two walls now: the agent's own path policy (core/path_policy.py) asks
    # about or refuses most of these before the tool runs -- the real home is
    # outside the temp home -- and this script's wrapper catches whatever
    # gets past it. Every one must have been stopped by one of the two.
    from agent.core import path_policy

    policy_stops = [
        path_policy.classify(str(outside), "write").zone,
        path_policy.classify(str(outside), "move_into").zone,
    ]
    check(
        len(Rec.violations) + sum(zone != "free" for zone in policy_stops) >= 4,
        f"sandbox: each of those was stopped ({len(Rec.violations)} by the sandbox, {policy_stops} from the path policy)",
    )
    Rec.violations.clear()
    for cmd in ("echo hi > ~/Documents/plan.md", "rm -rf ~/Documents", f"git -C {shlex.quote(str(AGENT_DIR))} status",
                "git -C ~/Projects/atlas status; rm -rf ~", "cd ~/Projects/atlas && git config --global user.name x",
                "git -C ~/Projects/atlas commit -m \"$(whoami)\"", "open ~/Documents/run-me.command",
                "sh ~/Documents/run-me.command", "git -C ~/Projects/atlas pull --upload-pack=/bin/sh",
                "git -C ~/Projects/atlas log --output=/tmp/x", "git clone https://github.com/pallets/flask ~/Projects/flask"):
        result = await call("run_command", cmd=cmd)
        check(result["ok"] is False and "declined" in result.get("error", ""), f"sandbox: declined at the prompt: {cmd[:60]}")
    check((HOME / "Documents" / "plan.md").read_text().startswith("ORIGINAL PLAN") and Rec.commands == [],
          "sandbox: none of them ran")
    result = await call("run_command", cmd="git -C ~/Projects/atlas status --porcelain")
    check(result["ok"] and "app.py" in result["stdout"], "sandbox: git inside a sandbox repository does run")
    for tool, args in (("focus_app", {"name": "Notes"}), ("type_text", {"text": "x"}), ("press_keys", {"keys": "cmd+n"})):
        result = await call(tool, **args)
        check(result["ok"] is False and Rec.gui == [], f"sandbox: {tool} is declined and never reaches the keyboard")
    result = await call("trash_file", path="~/Desktop/todo.txt")
    check(result["ok"] and not (REAL_HOME / ".Trash" / "todo.txt").exists() and len(list((TMP / "Trash").iterdir())) == 1,
          "sandbox: trash_file moves into the sandbox's own Trash")
    shutil.rmtree(TMP / "Trash")
    check(Rec.violations == [], "sandbox: a declined command is not a violation; reaching the tool with one would be")


def real_machine_untouched(before) -> None:
    after = {name: sorted(p.name for p in (REAL_HOME / name).iterdir()) if (REAL_HOME / name).exists() else None
             for name in ("Desktop", "Downloads", "Documents")}
    check(after == before, "the real Desktop, Downloads and Documents hold exactly what they held before")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("names", nargs="*", help="scenarios to run (default: all)")
    parser.add_argument("--scripted", action="store_true", help="replay written plans; no model, no cost")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="do not print each request, tool call and reply")
    args = parser.parse_args()
    if args.list:
        for name, domain, _ in SCENARIOS:
            print(f"{domain:<7} {name}")
        return
    unknown = set(args.names) - {name for name, _, _ in SCENARIOS}
    if unknown:
        print(f"no such scenario: {sorted(unknown)}")
        sys.exit(2)
    missing = sorted(set(TOOLS) - REAL - set(STUBS))
    check(missing == [], f"every registered tool has a policy in this script ({missing})")
    before = {name: sorted(p.name for p in (REAL_HOME / name).iterdir()) if (REAL_HOME / name).exists() else None
              for name in ("Desktop", "Downloads", "Documents")}
    if args.scripted:
        backend = ScriptedBackend()
        print("SCRIPTED: no model is asked; this proves the sandbox and the assertions, not the routing\n")
    else:
        load_key()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is not set (expected in the repo-root .env); use --scripted for a free dry run")
            sys.exit(2)
        from agent.core.llm_anthropic import AnthropicBackend

        backend = AnthropicBackend()

    async def everything():
        print("# sandbox", flush=True)
        await sandbox_holds()
        for name, domain, build in SCENARIOS:
            if args.names and name not in args.names:
                continue
            print(f"\n# {domain}: {name}", flush=True)
            try:
                await run_scenario(name, build, backend, args.scripted, not args.quiet)
            except Exception as e:  # one scenario blowing up must not hide the rest
                check(False, f"{name}: ran to completion ({type(e).__name__}: {str(e)[:200]})")

    try:
        asyncio.run(everything())
        real_machine_untouched(before)
    finally:
        os.chdir("/")
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{passed} passed, {len(failures)} failed")
    if failures:
        for label in failures:
            print(f"  FAIL {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
