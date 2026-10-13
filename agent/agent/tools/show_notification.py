from agent.core import bounded

TIMEOUT = 10.0


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def show_notification(title: str, message: str) -> dict:
    script = f'display notification "{_escape(message)}" with title "{_escape(title)}"'
    code, _, stderr = bounded.run(["osascript", "-e", script], timeout=TIMEOUT)
    if code != 0:
        return {"ok": False, "error": stderr or "could not show the notification"}
    return {"ok": True, "message": "notification shown"}
