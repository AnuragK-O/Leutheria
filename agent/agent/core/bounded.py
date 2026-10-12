"""Hard deadlines for tool work that could otherwise hang.

dispatch() calls a tool function directly on the event loop (dispatcher.
_run_tool), so however long a tool blocks is how long the read loop -- and
with it confirmations and management messages -- is stalled. Subprocess and
socket timeouts bound most of that, but not all of it: a socket timeout does
not cover DNS resolution, and a requests timeout is per read, not per request.
These helpers put one ceiling over the whole call.
"""

import subprocess
import threading


class DeadlineExceeded(Exception):
    pass


def call_with_deadline(fn, seconds: float):
    """Run fn() in a daemon thread and return its result, or raise
    DeadlineExceeded after `seconds`. The thread is abandoned, not killed --
    callers must only pass work that is harmless to leave running (a read, a
    connect), never something with a side effect that matters."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:  # re-raised in the caller's thread
            box["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise DeadlineExceeded(f"took longer than {seconds:g} seconds")
    if "error" in box:
        raise box["error"]
    return box["value"]


def run(argv: list, timeout: float = 3.0) -> tuple:
    """Run a fixed argv (never a shell string) and return (returncode,
    stdout, stderr). A missing binary or a timeout comes back as a non-zero
    code with the reason in stderr rather than raising, so a read-only tool
    can report what it could find instead of failing outright."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out after {timeout:g} seconds"
    except OSError as e:
        return 127, "", f"{argv[0]}: {e}"
    return result.returncode, result.stdout, result.stderr.strip()
