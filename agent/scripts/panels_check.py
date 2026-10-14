"""Overlay panels, agent side (plans/overlay-panels-spec.md): the show_panel /
hide_panel built-in skills and the list_timers control message.

No model, no microphone, no sound, no server port: the skills run against a
fake websocket, both directly and through dispatch(), and control.handle() is
called the way server.py's read loop calls it. Local state goes to a temp dir.

    ./.venv/bin/python scripts/panels_check.py
"""

import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

TMP = Path(tempfile.mkdtemp(prefix="leutheria_panels_check_")).resolve()

from agent.core import logging_util  # noqa: E402

logging_util.LOG_DIR = TMP / "logs"
logging_util.LOG_FILE = logging_util.LOG_DIR / "events.jsonl"

from agent.core import preferences  # noqa: E402

preferences.PREFS_FILE = TMP / "preferences.json"
preferences._overrides.clear()

from agent.core import control, server, timers  # noqa: E402
from agent.core.dispatcher import dispatch  # noqa: E402
from agent.skills import panels  # noqa: E402
from agent.skills.registry import BUILTIN_SKILLS, SKILLS, anthropic_skill_schemas  # noqa: E402
from agent.tools.list_timers import list_timers  # noqa: E402
from agent.tools.registry import TOOLS  # noqa: E402

failures = []


def check(ok: bool, label: str) -> None:
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


def events(kind: str) -> list:
    if not logging_util.LOG_FILE.exists():
        return []
    rows = [json.loads(line) for line in logging_util.LOG_FILE.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("type") == kind]


class FakeSocket:
    def __init__(self, closed: bool = False):
        self.sent = []
        self.closed = closed

    async def send(self, message):
        if self.closed:
            raise ConnectionError("connection closed")
        self.sent.append(json.loads(message))


def run(coro):
    return asyncio.run(coro)


# --- registry ---
print("# registry")
for name in ("show_panel", "hide_panel"):
    entry = SKILLS.get(name, {})
    check(name in SKILLS and name in BUILTIN_SKILLS, f"{name} is a built-in skill")
    check(name not in TOOLS, f"{name} doesn't share a name with a tool")
    check(callable(entry.get("fn")), f"{name} has a run function")
    check(any(s["name"] == name for s in anthropic_skill_schemas()), f"{name} is described to the model")
    text = entry.get("description", "") + " ".join(
        p.get("description", "") for p in entry.get("input_schema", {}).get("properties", {}).values()
    )
    # Spoken back and read as prompt: no markdown, no escaped newlines.
    check(not re.search(r"[`*#\[\]\\]|\n", text), f"{name}: description is plain prose")
show_text = SKILLS["show_panel"]["description"]
check(all(p in show_text for p in panels.PANELS), "show_panel description names every panel id")
check("Not for answering a question in words" in show_text, "show_panel description says what it's not for")
check(SKILLS["show_panel"]["input_schema"]["properties"]["panel"]["enum"] == list(panels.PANELS), "panel enum is the spec's ids")
check(panels.PANELS == ("chat", "skills", "activity", "permissions", "timers", "voice"), "panel ids match the spec")
check("panel" not in server._MIRRORED_TYPES, "panel messages are not mirrored to overlays")

# --- show_panel ---
print("\n# show_panel")
ws = FakeSocket()
result = run(panels.show({"panel": "skills", "query": "  git "}, ws, {}))
check(result.get("ok") is True, "show skills succeeds")
check(ws.sent == [{"type": "panel", "action": "show", "panel": "skills", "query": "git"}], "exact message shape, query trimmed")
ws = FakeSocket()
run(panels.show({"panel": "timers"}, ws, {}))
check(ws.sent == [{"type": "panel", "action": "show", "panel": "timers"}], "no query key when none is given")
ws = FakeSocket()
run(panels.show({"panel": "chat", "query": "   "}, ws, {}))
check("query" not in ws.sent[0], "blank query is dropped")

ws = FakeSocket()
result = run(panels.show({"panel": "calendar"}, ws, {}))
check(result.get("ok") is False and "chat" in result.get("error", ""), "unknown panel refused, error lists the ids")
check(ws.sent == [], "unknown panel sends nothing")
check(any(e.get("panel") == "calendar" for e in events("panel_unknown")), "unknown panel is logged")
result = run(panels.show({}, FakeSocket(), {}))
check(result.get("ok") is False, "missing panel refused")

result = run(panels.show({"panel": "skills"}, None, {}))
check(result.get("ok") is False and "isn't open" in result.get("error", ""), "no UI connected: ok false, app isn't open")
result = run(panels.show({"panel": "skills"}, FakeSocket(closed=True), {}))
check(result.get("ok") is False and "isn't open" in result.get("error", ""), "UI closed mid-command: ok false, not raised")

# --- hide_panel ---
print("\n# hide_panel")
ws = FakeSocket()
check(run(panels.hide({"panel": "chat"}, ws, {})).get("ok") is True, "hide chat succeeds")
check(ws.sent == [{"type": "panel", "action": "hide", "panel": "chat"}], "hide message shape")
ws = FakeSocket()
run(panels.hide({}, ws, {}))
check(ws.sent == [{"type": "panel", "action": "hide", "panel": "all"}], "hide with no panel hides all")
ws = FakeSocket()
check(run(panels.hide({"panel": "nope"}, ws, {})).get("ok") is False and ws.sent == [], "hide unknown panel refused")
check(run(panels.hide({}, None, {})).get("ok") is False, "hide with no UI: ok false")

# --- through dispatch() ---
print("\n# dispatch")
ws = FakeSocket()
result = run(dispatch("show_panel", {"panel": "activity"}, ws, {}))
check(result.get("ok") is True, "dispatch(show_panel) succeeds")
panel_msgs = [m for m in ws.sent if m.get("type") == "panel"]
check(panel_msgs == [{"type": "panel", "action": "show", "panel": "activity"}], "dispatch sends one panel message")
check(not any(m.get("type") == "confirmation_required" for m in ws.sent), "no confirmation asked")
preferences.set_preference("show_panel", enabled=False)
ws = FakeSocket()
result = run(dispatch("show_panel", {"panel": "activity"}, ws, {}))
check(result.get("ok") is False and not any(m.get("type") == "panel" for m in ws.sent), "disabled show_panel doesn't run")
check(not any(s["name"] == "show_panel" for s in anthropic_skill_schemas()), "disabled show_panel isn't described")
preferences.clear("show_panel")

# --- list_timers control ---
print("\n# list_timers control")
check("list_timers" in control.CONTROL_TYPES, "list_timers is a control type")
result = control.handle({"type": "list_timers", "request_id": "7"})
check(result.get("ok") is True and result.get("timers") == [] and result.get("count") == 0, "no timers: empty list")


async def with_timers():
    timers.create(600, label="tea")
    timers.create(60)
    try:
        return control.handle({"type": "list_timers"}), list_timers()
    finally:
        timers.cancel(cancel_all=True)


via_control, via_tool = run(with_timers())
check(via_control.get("count") == 2 and [t["label"] for t in via_control["timers"]] == ["", "tea"], "two timers, soonest first")
strip = lambda r: {k: v for k, v in r.items() if k != "timers"}  # noqa: E731
check(set(via_control) == set(via_tool) and strip(via_control)["note"] == strip(via_tool)["note"], "same shape as the list_timers tool")
check(set(via_control["timers"][0]) == set(via_tool["timers"][0]), "timer entries have the tool's fields")
log_text = logging_util.LOG_FILE.read_text() if logging_util.LOG_FILE.exists() else ""
check('"tool": "list_timers"' not in log_text, "control read isn't logged as a tool call")
json.dumps(via_control)
check(True, "control result is JSON-serialisable")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all panel checks passed")
