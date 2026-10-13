from agent.core import bounded

TIMEOUT = 10.0  # System Events can be slow to wake the first time


def list_running_apps() -> dict:
    script = "tell application \"System Events\" to get name of (processes where background only is false)"
    code, stdout, stderr = bounded.run(["osascript", "-e", script], timeout=TIMEOUT)
    if code != 0:
        return {"ok": False, "error": stderr or "could not list the running apps"}
    apps = [name.strip() for name in stdout.strip().split(",") if name.strip()]
    return {"ok": True, "apps": apps}
