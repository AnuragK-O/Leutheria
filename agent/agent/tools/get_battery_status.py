import re

from agent.core import bounded


def get_battery_status() -> dict:
    code, stdout, stderr = bounded.run(["pmset", "-g", "batt"], timeout=5.0)
    if code != 0:
        return {"ok": False, "error": stderr or "could not read the battery status"}

    match = re.search(r"(\d+)%;\s*([a-zA-Z ]+);", stdout)
    if not match:
        # Desktops with no battery report no percentage line at all.
        return {"ok": True, "has_battery": False}

    percent = int(match.group(1))
    state = match.group(2).strip()
    return {"ok": True, "has_battery": True, "percent": percent, "state": state}
