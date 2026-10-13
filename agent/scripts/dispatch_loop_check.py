"""Check that dispatch() keeps the event loop free while a tool runs.

    cd agent
    ./.venv/bin/python scripts/dispatch_loop_check.py

What it proves, all through the real dispatch():

  - a slow tool runs on a worker thread: a ticking coroutine keeps ticking
    and a confirmation for a *different* call is asked and answered meanwhile;
  - a tool flagged "thread": "main" in the registry still runs on the loop
    thread, and the flagged set is exactly the one that was measured;
  - an async tool function is awaited; any exception from any tool becomes a
    clean {"ok": False} and is logged; cancellation is not swallowed;
  - the check-then-write file tools never run two at once;
  - run_command times out, kills its whole process group, caps its output,
    and gives a waiting command end-of-file instead of hanging;
  - an unanswered confirmation sends confirmation_resolved by "timeout";
  - the older tools pass a subprocess timeout.

Nothing here opens the microphone, a port, a window or a banner: speech is
replaced, the tools that would open something are given a fake subprocess,
and state (event log, preferences, trust) is redirected to a temp dir. The
only real processes started are `echo`, `sleep`, `cat` and friends. Prints
one line per check and exits non-zero on the first failure.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

TMP = Path(tempfile.mkdtemp(prefix="leutheria_dispatch_check_")).resolve()

# Redirect local state before anything imports the registries.
from agent.core import logging_util  # noqa: E402

logging_util.LOG_DIR = TMP / "logs"
logging_util.LOG_FILE = logging_util.LOG_DIR / "events.jsonl"

from agent.core import preferences, trust  # noqa: E402

preferences.PREFS_FILE = TMP / "preferences.json"
preferences._overrides.clear()
trust.TRUST_FILE = TMP / "trusted_commands.json"
trust._permanent_trust.clear()
trust._session_trust.clear()

from agent.core import bounded, dispatcher, timers  # noqa: E402
from agent.core.dispatcher import dispatch  # noqa: E402
from agent.tools import run_command as run_command_tool  # noqa: E402
from agent.tools.registry import TOOLS  # noqa: E402

passed = 0
MAIN_THREAD = threading.main_thread()


def check(condition, label):
    global passed
    if not condition:
        print(f"FAIL  {label}")
        sys.exit(1)
    passed += 1
    print(f"ok    {label}")


def events(kind):
    if not logging_util.LOG_FILE.exists():
        return []
    records = [json.loads(line) for line in logging_util.LOG_FILE.read_text().splitlines()]
    return [r for r in records if r["type"] == kind]


spoken = []


async def fake_speak(websocket, text):
    spoken.append(text)


dispatcher.speak = fake_speak  # confirmation prompts are spoken; never aloud here


class FakeSocket:
    """Records what dispatch() sends. `answer` (None = never) is given to each
    confirmation as soon as it is asked, the way a click would."""

    def __init__(self, pending: dict, answer=True):
        self.pending = pending
        self.answer = answer
        self.sent = []

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message["type"] == "confirmation_required" and self.answer is not None:
            asyncio.get_running_loop().call_soon(
                self.pending[message["id"]].set_result, {"approved": self.answer, "remember": None}
            )

    def of_type(self, kind):
        return [m for m in self.sent if m["type"] == kind]


def register(name, fn, **extra):
    TOOLS[name] = {"fn": fn, "safety": "safe", "description": "check probe", "input_schema": {}, **extra}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- 1. the registry's thread flags ------------------------------------------------
print("# registry flags")
EXPECTED_MAIN = {
    "focus_app", "describe_ui", "press_keys", "type_text",  # NSWorkspace needs the main run loop
    "media_control",  # AppKit events; instant
    "set_timer", "list_timers", "cancel_timer",  # asyncio tasks
}
flags = {name: tool.get("thread") for name, tool in TOOLS.items() if "thread" in tool}
check(set(flags.values()) <= {"main"}, 'the only "thread" value in use is "main"')
check(set(flags) == EXPECTED_MAIN, f"exactly the measured tools are flagged main: {sorted(flags)}")
check(all(TOOLS[name]["safety"] != "scoped" or name in flags for name in TOOLS),
      "every scoped (GUI) tool is flagged main -- its focus check is only live there")
check(dispatcher._FS_WRITERS <= set(TOOLS) and not (dispatcher._FS_WRITERS & set(flags)),
      "every serialized file writer is a real tool, and none is a main-thread tool")


# --- 2. the loop stays responsive --------------------------------------------------
async def responsiveness():
    print("\n# a slow tool does not block the loop")
    ran_on = {}

    def slow_probe(seconds: float = 1.0) -> dict:
        ran_on["slow"] = threading.current_thread()
        time.sleep(seconds)
        return {"ok": True, "slept": seconds}

    register("_slow_probe", slow_probe)

    ticks = []

    async def ticker():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    tick_task = asyncio.create_task(ticker())
    started = time.monotonic()
    slow_task = asyncio.create_task(dispatch("_slow_probe", {"seconds": 1.0}))
    await asyncio.sleep(0.05)

    # While it runs: another call that needs a confirmation is asked,
    # answered, and finished.
    pending = {}
    socket = FakeSocket(pending, answer=True)
    confirmed = await dispatch("run_command", {"cmd": "echo answered"}, socket, pending)
    confirmed_at = time.monotonic() - started
    check(confirmed == {"ok": True, "stdout": "answered", "stderr": ""},
          "a confirmation for another call was asked, answered and run meanwhile (result shape unchanged)")
    check(len(socket.of_type("confirmation_required")) == 1 and not slow_task.done(),
          f"...and it finished after {confirmed_at:.2f}s, while the slow tool was still running")

    result = await slow_task
    elapsed = time.monotonic() - started
    tick_task.cancel()
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    check(result == {"ok": True, "slept": 1.0}, "the slow tool's own result arrived intact")
    check(ran_on["slow"] is not MAIN_THREAD, "it ran on a worker thread, not the loop thread")
    check(len(ticks) > 50 and max(gaps) < 0.25,
          f"a 10 ms ticker ran {len(ticks)} times in {elapsed:.2f}s, longest gap {max(gaps) * 1000:.0f} ms")

    # The same tool flagged main really does block -- which is what the flag
    # means, and proves the ticker above would have caught an inline call.
    register("_slow_main_probe", slow_probe, thread="main")
    ticks.clear()
    tick_task = asyncio.create_task(ticker())
    await asyncio.sleep(0.03)
    await dispatch("_slow_main_probe", {"seconds": 0.4})
    await asyncio.sleep(0.03)
    tick_task.cancel()
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    check(ran_on["slow"] is MAIN_THREAD, 'a tool flagged "thread": "main" runs on the loop thread')
    check(max(gaps) > 0.35, f"...and holds the loop while it does (gap {max(gaps) * 1000:.0f} ms): the ticker can tell")

    print("\n# main-thread tools still work through dispatch")
    timers.reset()
    made = await dispatch("set_timer", {"seconds": 30, "label": "probe"})
    check(made.get("ok") and made.get("label") == "probe", "set_timer creates its asyncio task (it would be refused off the loop)")
    listed = await dispatch("list_timers", {})
    check(listed.get("count") == 1, "list_timers sees it")
    gone = await dispatch("cancel_timer", {"label": "probe"})
    check(gone.get("ok") and timers.active() == [], "cancel_timer cancels it")
    box = {}
    worker = threading.Thread(target=lambda: box.update(result=TOOLS["set_timer"]["fn"](seconds=30)))
    worker.start()
    worker.join()
    check(box["result"].get("ok") is False, "called from a worker thread set_timer refuses -- which is why it is flagged")
    timers.reset()

    print("\n# async tool functions, exceptions, cancellation")

    async def async_probe(value: str) -> dict:
        await asyncio.sleep(0.01)
        return {"ok": True, "echo": value, "thread_is_main": threading.current_thread() is MAIN_THREAD}

    register("_async_probe", async_probe)
    check(await dispatch("_async_probe", {"value": "hi"}) == {"ok": True, "echo": "hi", "thread_is_main": True},
          "an async tool function is awaited on the loop")

    def boom(kind: str = "runtime") -> dict:
        if kind == "os":
            raise FileExistsError(17, "File exists", "/somewhere")
        raise RuntimeError("the tool blew up")

    register("_boom_worker", boom)
    register("_boom_main", boom, thread="main")
    before = len(events("tool_error"))
    for name in ("_boom_worker", "_boom_main"):
        result = await dispatch(name, {})
        check(result.get("ok") is False and "the tool blew up" in result.get("error", "") and name in result["error"],
              f"{name}: an exception becomes a clean error naming the tool ({result.get('error')})")
    result = await dispatch("_boom_worker", {"kind": "os"})
    check(result.get("ok") is False and "FileExistsError" in result["error"], "an OSError is caught too, not only TypeError")
    logged = events("tool_error")[before:]
    check(len(logged) == 3 and all("Traceback" in r["traceback"] for r in logged) and logged[0]["tool"] == "_boom_worker",
          "each one is logged as tool_error with its traceback")
    result = await dispatch("_boom_worker", {"nonsense": 1})
    check(result.get("ok") is False and "unexpected keyword" in result["error"],
          "wrong arguments still come back as Python's own message")
    result = await dispatch("list_files", {"path": str(TMP), "bogus": True})
    check(result.get("ok") is False and "bogus" in result["error"], "the same for a real tool")

    finished = threading.Event()

    def lingering() -> dict:
        time.sleep(0.3)
        finished.set()
        return {"ok": True}

    register("_lingering", lingering)
    task = asyncio.create_task(dispatch("_lingering", {}))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
        cancelled = False
    except asyncio.CancelledError:
        cancelled = True
    check(cancelled, "cancelling a request while its tool runs raises CancelledError (not swallowed into a result)")
    await asyncio.sleep(0.4)
    check(finished.is_set(), "the abandoned thread finishes on its own and its late result is dropped without error")
    check((await dispatch("list_files", {"path": str(TMP)})).get("ok") is True, "the loop is fine afterwards")

    print("\n# file writers are serialized, everything else is concurrent")
    active = {"writers": 0, "max_writers": 0, "readers": 0, "max_readers": 0}
    guard = threading.Lock()

    def tracked(kind):
        def fn(**_):
            with guard:
                active[kind] += 1
                active[f"max_{kind}"] = max(active[f"max_{kind}"], active[kind])
            time.sleep(0.15)
            with guard:
                active[kind] -= 1
            return {"ok": True}
        return fn

    real = {name: TOOLS[name]["fn"] for name in ("copy_file", "move_file", "write_new_file")}
    for name in real:
        TOOLS[name]["fn"] = tracked("writers")
    register("_reader_a", tracked("readers"))
    register("_reader_b", tracked("readers"))
    try:
        started = time.monotonic()
        await asyncio.gather(
            dispatch("copy_file", {}), dispatch("move_file", {}), dispatch("write_new_file", {}),
            dispatch("_reader_a", {}), dispatch("_reader_b", {}),
        )
        took = time.monotonic() - started
    finally:
        for name, fn in real.items():
            TOOLS[name]["fn"] = fn
    check(active["max_writers"] == 1, "three check-then-write file tools dispatched at once never overlapped")
    check(active["max_readers"] == 2, "two other tools dispatched at once did run together")
    check(0.4 < took < 1.5, f"the batch took {took:.2f}s: writers one after another, readers alongside")

    print("\n# the event log from many threads")
    before = len(events("thread_probe"))
    threads = [
        threading.Thread(target=lambda n=n: [logging_util.log_event("thread_probe", n=n, pad="x" * 5000) for _ in range(40)])
        for n in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(len(events("thread_probe")) - before == 320, "320 records written from 8 threads are 320 valid JSON lines")
    logging_util.log_event("odd_field", path=TMP, error=ValueError("x"))
    check(events("odd_field")[-1]["error"] == "x", "a record with a non-JSON field is still written")


asyncio.run(responsiveness())


# --- 3. run_command's limits -------------------------------------------------------
print("\n# run_command: shape, timeout, kill, output cap")
run_command = run_command_tool.run_command
check(run_command("echo hi") == {"ok": True, "stdout": "hi", "stderr": ""}, "a normal command returns exactly ok/stdout/stderr")
failed = run_command("echo oops >&2; exit 3")
check(failed == {"ok": False, "stdout": "", "stderr": "oops"}, "a failing command: ok False, stderr kept, same shape")
check(run_command("printf '\\xff\\xfe ok'").get("ok") is True, "output that is not UTF-8 does not raise")
check(run_command("").get("ok") is False and run_command(None).get("ok") is False, "no command is a clean error")

check(run_command_tool._timeout(None) == run_command_tool.DEFAULT_TIMEOUT_S == 300, "the default limit is 300 seconds")
check(run_command_tool._timeout(99999) == run_command_tool.MAX_TIMEOUT_S == 1800, "timeout_s is clamped to the 1800 second ceiling")
check(run_command_tool._timeout(0) == run_command_tool.MIN_TIMEOUT_S, "...and to at least a second")
check(run_command("echo hi", timeout_s="soon").get("ok") is False and run_command("echo hi", timeout_s=True).get("ok") is False,
      "a timeout_s that is not a number is refused before anything runs")

started = time.monotonic()
check(run_command("cat; echo done") == {"ok": True, "stdout": "done", "stderr": ""} and time.monotonic() - started < 2,
      "a command that reads stdin gets end-of-file at once instead of waiting")

pid_file = TMP / "child.pid"
started = time.monotonic()
timed = run_command(f"sleep 60 & echo $! > '{pid_file}'; echo begun; wait", timeout_s=1)
took = time.monotonic() - started
child = int(pid_file.read_text())
check(timed.get("ok") is False and timed.get("timed_out") is True and "1 second" in timed["error"],
      f"a command past its limit is stopped with a clear error ({timed['error'][:60]}...)")
check(timed["stdout"] == "begun", "what it printed before the limit is kept")
check(took < 1 + 2 * run_command_tool.KILL_GRACE_S + 1, f"it came back in {took:.1f}s")
time.sleep(0.2)
check(not pid_alive(child), "the grandchild it started (sleep 60) was killed with it -- the whole process group")

pid_file.unlink()
started = time.monotonic()
stubborn = run_command(f"trap '' TERM; echo $$ > '{pid_file}'; while :; do sleep 0.2; done", timeout_s=1)
shell_pid = int(pid_file.read_text())
check(stubborn.get("timed_out") is True and not pid_alive(shell_pid),
      f"a command that ignores SIGTERM is SIGKILLed ({time.monotonic() - started:.1f}s)")

started = time.monotonic()
background = run_command("sleep 4 & echo started")
check(background == {"ok": True, "stdout": "started", "stderr": ""} and time.monotonic() - started < 3,
      "a command that leaves something running in the background returns without waiting for it")

big = run_command("head -c 3000000 /dev/zero | tr '\\0' 'x'; echo; echo THE-END")
keep = run_command_tool.HEAD_CHARS + run_command_tool.TAIL_CHARS
check(big.get("ok") is True and big.get("truncated") is True and len(big["stdout"]) < keep + 200,
      f"3 MB of output is cut to {len(big['stdout'])} characters and marked truncated")
check(big["stdout"].startswith("xxxx") and big["stdout"].endswith("THE-END") and "left out" in big["stdout"],
      "the beginning and the end are both kept, with a note of what was left out between")
started = time.monotonic()
huge = run_command("head -c 200000000 /dev/zero")
check(huge.get("truncated") is True and len(huge["stdout"]) < keep + 200,
      f"200 MB of output neither hangs nor is kept ({time.monotonic() - started:.1f}s)")
edge = run_command(f"head -c {keep} /dev/zero | tr '\\0' 'y'")
check("truncated" not in edge and len(edge["stdout"]) == keep, "output exactly at the cap is returned whole")

holder = {}
runner = threading.Thread(target=lambda: holder.update(result=run_command("sleep 30")))
started = time.monotonic()
runner.start()
time.sleep(0.3)
check(run_command_tool.kill_running() == 1, "kill_running (agent shutdown) finds the command in flight")
runner.join(5)
check(not runner.is_alive() and holder["result"]["ok"] is False and time.monotonic() - started < 3,
      "...and kills it; the call returns as a failure")
check(run_command_tool.kill_running() == 0 and not run_command_tool._running, "nothing is left tracked")

description = TOOLS["run_command"]["description"]
check("5 minutes" in description and "1800" in description, "the description tells the model about the limit")
check("timeout_s" in TOOLS["run_command"]["input_schema"]["properties"]
      and TOOLS["run_command"]["input_schema"]["required"] == ["cmd"], "timeout_s is in the schema and optional")


async def through_dispatch():
    pending = {}
    socket = FakeSocket(pending, answer=True)
    started = time.monotonic()
    result = await dispatch("run_command", {"cmd": "sleep 30", "timeout_s": 1}, socket, pending)
    check(result.get("timed_out") is True and time.monotonic() - started < 6, "the timeout works through dispatch too")
    check(socket.of_type("confirmation_required")[0]["args"] == {"cmd": "sleep 30", "timeout_s": 1},
          "the confirmation card shows the requested timeout with the command")

    # --- 4. a confirmation nobody answers -----------------------------------------
    print("\n# an unanswered confirmation")
    marker = TMP / "must-not-exist"
    pending = {}
    socket = FakeSocket(pending, answer=None)
    real_timeout = dispatcher.CONFIRMATION_TIMEOUT
    dispatcher.CONFIRMATION_TIMEOUT = 0.2
    try:
        started = time.monotonic()
        result = await dispatch("run_command", {"cmd": f"touch '{marker}'"}, socket, pending)
        took = time.monotonic() - started
    finally:
        dispatcher.CONFIRMATION_TIMEOUT = real_timeout
    asked = socket.of_type("confirmation_required")
    resolved = socket.of_type("confirmation_resolved")
    check(0.15 < took < 1.0 and result.get("ok") is False and "timed out" in result["error"],
          f"it times out ({took:.2f}s) and the command is reported declined")
    check(not marker.exists(), "the command did not run")
    check(len(asked) == 1 and resolved == [{"type": "confirmation_resolved", "id": asked[0]["id"], "approved": False, "by": "timeout"}],
          'the UI is sent confirmation_resolved {approved: false, by: "timeout"} for the same id')
    check(socket.sent.index(resolved[0]) > socket.sent.index(asked[0]) and pending == {},
          "after the card, exactly once, and nothing is left pending")
    check(events("confirmation_timed_out")[-1]["id"] == asked[0]["id"], "it is logged as confirmation_timed_out")

    class GoneSocket(FakeSocket):
        async def send(self, raw):
            if json.loads(raw)["type"] == "confirmation_resolved":
                raise ConnectionError("window closed")
            await super().send(raw)

    dispatcher.CONFIRMATION_TIMEOUT = 0.1
    try:
        pending = {}
        result = await dispatch("run_command", {"cmd": "true"}, GoneSocket(pending, answer=None), pending)
    finally:
        dispatcher.CONFIRMATION_TIMEOUT = real_timeout
    check(result.get("ok") is False and pending == {}, "if the window is gone by then, the decline still stands")

    pending = {}
    socket = FakeSocket(pending, answer=True)
    await dispatch("run_command", {"cmd": "true"}, socket, pending)
    check(socket.of_type("confirmation_resolved") == [], "an answered confirmation gets no timeout message")
    check(dispatcher.CONFIRMATION_TIMEOUT == 120, "the real timeout is still 120 seconds")


asyncio.run(through_dispatch())


# --- 5. the older tools have subprocess timeouts ---------------------------------
print("\n# older tools: every subprocess call is bounded")
calls = []
mode = {"hang": False}
real_run = subprocess.run


def fake_run(argv, **kwargs):
    calls.append({"argv": list(argv), "timeout": kwargs.get("timeout")})
    if mode["hang"]:
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
    stdout = " Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t80%; discharging; 3:00 remaining\n"
    return subprocess.CompletedProcess(argv, 0, stdout if kwargs.get("text") else stdout.encode(), "")


OLD_TOOLS = {
    "get_battery_status": {},
    "open_app": {"name": "Nothing Real"},
    "open_url": {"url": "https://example.invalid/"},
    "show_notification": {"title": "t", "message": "m"},
    "get_clipboard": {},
    "set_clipboard": {"text": "never copied"},
    "list_running_apps": {},
}
subprocess.run = fake_run  # bounded.run and the tools all reach it through the module
try:
    for name, args in OLD_TOOLS.items():
        calls.clear()
        mode["hang"] = False
        result = TOOLS[name]["fn"](**args)
        check(result.get("ok") is True and len(calls) == 1 and (calls[0]["timeout"] or 0) > 0,
              f"{name} passes a timeout ({calls[0]['timeout']:g}s)")
        mode["hang"] = True
        result = TOOLS[name]["fn"](**args)
        check(result.get("ok") is False and result.get("error"), f"{name} timing out is a clean error: {result.get('error')}")
finally:
    subprocess.run = real_run
check(bounded.run(["true"])[0] == 0, "the real subprocess.run is back")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")
