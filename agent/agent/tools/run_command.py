"""Run one shell command, bounded in time and in how much output it keeps.

The command is still arbitrary -- that is why this is the one "destructive"
tool and always confirms -- but the *call* is not open-ended any more:

  time     DEFAULT_TIMEOUT_S unless the caller passes timeout_s, which is
           clamped to MAX_TIMEOUT_S. dispatch() runs this on a worker thread,
           so the limit is not about keeping the event loop alive; it is about
           a command that will never finish on its own (waiting for input, a
           dev server, a hung network mount) holding a request open forever.
           Five minutes covers the long commands people really approve (git
           clone, npm install, a test run); a build that needs longer asks for
           it explicitly, and the number is in the args the user confirms.
  kill     the child gets its own session, so its process group is exactly
           "this command and everything it started". On timeout the whole
           group gets SIGTERM, then SIGKILL after KILL_GRACE_S -- killing only
           the shell would leave `sleep 600` (or the compiler, or npm's node)
           running with nobody waiting for it.
  stdin    /dev/null, and no controlling terminal: a command that stops to
           ask a question gets end-of-file and fails at once instead of
           sitting there until the timeout.
  output   each stream keeps its first HEAD_CHARS and last TAIL_CHARS bytes
           and counts the rest, so `cat huge.log` can neither exhaust memory
           nor be sent whole to the model. Both ends are kept because the
           start says what ran and the end says how it failed.

Commands still in flight are tracked so the agent can kill them on its way
out (kill_running, called from server.main): with their own session they no
longer die with the agent's terminal on Ctrl-C.
"""

import os
import signal
import subprocess
import threading
import time
from collections import deque

DEFAULT_TIMEOUT_S = 300.0
MAX_TIMEOUT_S = 1800.0
MIN_TIMEOUT_S = 1.0
KILL_GRACE_S = 2.0  # between SIGTERM and SIGKILL
DRAIN_GRACE_S = 1.0  # to collect output after the shell has exited

HEAD_CHARS = 8_000
TAIL_CHARS = 12_000

_running: set = set()  # Popen objects of commands in flight
_running_lock = threading.Lock()


class _Capture:
    """Drains one pipe to the end, keeping only the head and the tail. It
    must keep reading after the cap is reached: a child whose pipe fills up
    blocks, and would then look like a hang rather than a noisy command."""

    def __init__(self, pipe):
        self._pipe = pipe
        self._head = bytearray()
        self._tail = deque()
        self._tail_size = 0
        self.total = 0
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._pipe.read1(65536)
                if not chunk:
                    break
                self.total += len(chunk)
                room = HEAD_CHARS - len(self._head)
                if room > 0:
                    self._head += chunk[:room]
                    chunk = chunk[room:]
                if chunk:
                    self._tail.append(chunk)
                    self._tail_size += len(chunk)
                    while self._tail_size - len(self._tail[0]) >= TAIL_CHARS:
                        self._tail_size -= len(self._tail.popleft())
        except (OSError, ValueError):
            pass
        finally:
            # Closed here, by the only thread that reads it: closing from
            # another thread waits for a read in progress, which for a pipe a
            # background process still holds open means waiting for that
            # process (measured: `sleep 4 & echo hi` took 4 s to return).
            try:
                self._pipe.close()
            except OSError:
                pass

    def finish(self, wait: float) -> None:
        self._thread.join(wait)

    @property
    def truncated(self) -> bool:
        return self.total > HEAD_CHARS + TAIL_CHARS

    def text(self) -> str:
        head = bytes(self._head)
        tail = b"".join(self._tail)[-TAIL_CHARS:]
        if not self.truncated:
            return (head + tail).decode("utf-8", "replace").strip()
        omitted = self.total - len(head) - len(tail)
        return (
            head.decode("utf-8", "replace")
            + f"\n[... {omitted} bytes of output left out ...]\n"
            + tail.decode("utf-8", "replace")
        ).strip()


def _kill_group(proc) -> None:
    """SIGTERM the command's whole process group, then SIGKILL what's left."""
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, KILL_GRACE_S)):
        try:
            os.killpg(proc.pid, sig)  # pid == pgid: the child leads its own session
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(wait)
        except subprocess.TimeoutExpired:
            continue
        # The shell is gone, but a child that ignored SIGTERM may not be.
        if sig == signal.SIGTERM:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        return


def kill_running() -> int:
    """Kill every command still in flight. For shutdown; returns how many."""
    with _running_lock:
        procs = list(_running)
    for proc in procs:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return len(procs)


def _timeout(timeout_s) -> float:
    if timeout_s is None:
        return DEFAULT_TIMEOUT_S
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s != timeout_s:
        raise ValueError("timeout_s must be a number of seconds")
    return max(MIN_TIMEOUT_S, min(float(timeout_s), MAX_TIMEOUT_S))


def run_command(cmd: str, timeout_s: float = None) -> dict:
    if not isinstance(cmd, str) or not cmd.strip():
        return {"ok": False, "error": "no command was given"}
    try:
        limit = _timeout(timeout_s)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as e:
        return {"ok": False, "error": f"could not start the command: {e}"}

    with _running_lock:
        _running.add(proc)
    out, err = _Capture(proc.stdout), _Capture(proc.stderr)
    timed_out = False
    try:
        try:
            proc.wait(limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
        # Normally immediate. It isn't when the command left something running
        # in the background that still holds the pipe (`server &`): take what
        # has been written so far rather than waiting for that to exit too.
        deadline = time.monotonic() + DRAIN_GRACE_S
        out.finish(DRAIN_GRACE_S)
        err.finish(max(0.0, deadline - time.monotonic()))
    finally:
        with _running_lock:
            _running.discard(proc)

    result = {"ok": proc.returncode == 0 and not timed_out, "stdout": out.text(), "stderr": err.text()}
    if out.truncated or err.truncated:
        result["truncated"] = True
        result["note"] = (
            f"The output was long, so only the first {HEAD_CHARS} and last {TAIL_CHARS} bytes of each stream were kept."
        )
    if timed_out:
        result["timed_out"] = True
        result["error"] = (
            f"The command was stopped because it was still running after {limit:g} seconds. "
            f"Everything it started was killed. If it really needs longer, run it again with "
            f"timeout_s set, up to {MAX_TIMEOUT_S:g}."
        )
    return result
