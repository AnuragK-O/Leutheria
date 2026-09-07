import re
import subprocess


def get_battery_status() -> dict:
    result = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()}

    match = re.search(r"(\d+)%;\s*([a-zA-Z ]+);", result.stdout)
    if not match:
        # Desktops with no battery report no percentage line at all.
        return {"ok": True, "has_battery": False}

    percent = int(match.group(1))
    state = match.group(2).strip()
    return {"ok": True, "has_battery": True, "percent": percent, "state": state}
