import re

from agent.core import bounded

# The one place the volume tools reach the system, so a test can swap it for
# a fake and never touch the real volume (set_volume calls it through this
# module, not through its own import).
def _osascript(script: str) -> tuple:
    return bounded.run(["osascript", "-e", script], timeout=5.0)


def read_volume() -> dict:
    """{"level": 0-100, "muted": bool} for the current output device, or
    {"error": ...}. Uses the Standard Additions `volume settings` record, which
    needs no Automation permission (it talks to no other app)."""
    code, out, err = _osascript("get volume settings")
    if code != 0:
        return {"error": err or "could not read the volume"}
    level = re.search(r"output volume:(\d+)", out)
    muted = re.search(r"output muted:(true|false)", out)
    if not level:
        # An HDMI display or some USB/aggregate devices have no software
        # volume: macOS reports "output volume:missing value".
        return {"error": "the current sound output has no volume control the Mac can set"}
    return {"level": int(level.group(1)), "muted": bool(muted and muted.group(1) == "true")}


def describe(level: int, muted: bool) -> str:
    return f"Volume is at {level} percent" + (", and sound is muted." if muted else ".")


def get_volume() -> dict:
    state = read_volume()
    if "error" in state:
        return {"ok": False, "error": state["error"]}
    return {"ok": True, **state, "message": describe(state["level"], state["muted"])}
