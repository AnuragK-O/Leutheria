from agent.core import timers


def list_timers() -> dict:
    running = timers.active()
    if not running:
        message = "No timers are running."
    else:
        parts = [
            f"{t['label'] or 'timer ' + t['timer_id']} with {t['remaining']} left" for t in running
        ]
        count = f"{len(running)} timer{'' if len(running) == 1 else 's'} running"
        message = f"{count}: {'; '.join(parts)}."
    return {
        "ok": True,
        "count": len(running),
        "timers": running,
        "note": timers.LIFETIME_NOTE,
        "message": message,
    }
