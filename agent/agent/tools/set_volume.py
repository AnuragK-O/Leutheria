from agent.tools import get_volume as volume


def _whole(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if value != value or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return int(round(value))


def set_volume(level: int = None, change: int = None, muted: bool = None) -> dict:
    """Set the output volume to a level, nudge it by an amount, and/or mute.

    Safe because it is fully reversible and says how: the result carries the
    previous level and mute state. The mute state is always written
    explicitly, to whatever was asked or else to what it already was, so a
    level change never mutes or unmutes as a side effect of how macOS happens
    to treat `set volume`. The new state is read back rather than assumed.
    """
    if level is None and change is None and muted is None:
        return {"ok": False, "error": "give a level, a change, or muted"}
    if level is not None and change is not None:
        return {"ok": False, "error": "give either level or change, not both"}
    if muted is not None and not isinstance(muted, bool):
        return {"ok": False, "error": "muted must be true or false"}
    try:
        if level is not None:
            level = _whole(level, "level", 0, 100)
        if change is not None:
            change = _whole(change, "change", -100, 100)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    before = volume.read_volume()
    if "error" in before:
        return {"ok": False, "error": before["error"]}

    target = before["level"]
    if level is not None:
        target = level
    elif change is not None:
        target = max(0, min(100, before["level"] + change))
    target_muted = before["muted"] if muted is None else muted

    # Both values are an int in 0..100 and a bool by this point -- nothing
    # from the caller reaches the script as text.
    script = f"set volume output volume {target} output muted {'true' if target_muted else 'false'}"
    code, _, err = volume._osascript(script)
    if code != 0:
        return {"ok": False, "error": err or "could not set the volume"}

    after = volume.read_volume()
    if "error" in after:
        return {"ok": False, "error": f"the volume was set but could not be read back: {after['error']}"}

    result = {
        "ok": True,
        "level": after["level"],
        "muted": after["muted"],
        "previous_level": before["level"],
        "previous_muted": before["muted"],
        "message": volume.describe(after["level"], after["muted"]),
    }
    if after["muted"] and muted is None and after["level"] > 0:
        result["note"] = "Sound is still muted, so nothing will be heard until it is unmuted."
    return result
