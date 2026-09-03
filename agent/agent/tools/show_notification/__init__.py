import subprocess


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def show_notification(title: str, message: str) -> dict:
    script = f'display notification "{_escape(message)}" with title "{_escape(title)}"'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()}
    return {"ok": True, "message": "notification shown"}
