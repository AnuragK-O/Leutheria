from agent.core import timers


def cancel_timer(timer_id: str = "", label: str = "", all: bool = False) -> dict:  # noqa: A002 -- the model-facing name
    """Cancel one timer (by id or label), the only one running, or all of
    them. Cancelling is the only thing this can do: a timer holds no data."""
    try:
        cancelled = timers.cancel(timer_id=str(timer_id or ""), label=label or "", cancel_all=all is True)
    except timers.TimerError as e:
        return {"ok": False, "error": str(e), "timers": timers.active()}

    if not cancelled:
        return {"ok": True, "cancelled": [], "remaining_count": 0, "message": "There were no timers to cancel."}
    names = [t["label"] or f"timer {t['timer_id']}" for t in cancelled]
    return {
        "ok": True,
        "cancelled": cancelled,
        "remaining_count": len(timers.active()),
        "message": f"Cancelled {', '.join(names)}.",
    }
