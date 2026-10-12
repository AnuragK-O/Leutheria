from agent.core import timers


def _number(value, name: str) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise timers.TimerError(f"{name} must be a number")
    if value < 0:
        raise timers.TimerError(f"{name} can't be negative")
    return float(value)


def set_timer(
    seconds: float = None, minutes: float = None, hours: float = None, label: str = "", message: str = ""
) -> dict:
    """Start a countdown. The three duration fields add up, so "an hour and a
    half" is hours=1, minutes=30; fractions are allowed (and are how the check
    script sets sub-second timers). See core/timers.py for what firing does."""
    try:
        total = _number(seconds, "seconds") + 60 * _number(minutes, "minutes") + 3600 * _number(hours, "hours")
        if seconds is None and minutes is None and hours is None:
            raise timers.TimerError("give a duration in seconds, minutes or hours")
        timer = timers.create(total, label=label, message=message)
    except timers.TimerError as e:
        return {"ok": False, "error": str(e)}

    named = f"{timer['label']} timer" if timer["label"] else "Timer"
    return {
        "ok": True,
        **timer,
        "note": timers.LIFETIME_NOTE,
        "message": f"{named} set for {timer['duration']}, ending at {timer['ends_at']}.",
    }
