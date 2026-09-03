import subprocess


def list_running_apps() -> dict:
    script = "tell application \"System Events\" to get name of (processes where background only is false)"
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()}
    apps = [name.strip() for name in result.stdout.strip().split(",") if name.strip()]
    return {"ok": True, "apps": apps}
