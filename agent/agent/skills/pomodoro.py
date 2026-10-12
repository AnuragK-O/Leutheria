from agent.skills.system_steps import Steps, StepFailed, invalid, number


async def run(args: dict, websocket, pending: dict) -> dict:
    """One pomodoro: a focus timer, then a break timer that ends after it,
    and a banner saying both end times. Three steps: set_timer, set_timer,
    show_notification.

    Both timers are started up front (the break one simply runs for focus +
    break) rather than the second being chained off the first: a timer only
    notifies and speaks, it can't start anything. If the second can't be set
    the first is cancelled again, so a failed pomodoro leaves nothing running.
    """
    try:
        focus = number(args, "focus_minutes", 25, 0.001, 180)
        rest = number(args, "break_minutes", 5, 0.001, 60)
    except ValueError as e:
        return invalid(str(e))
    task = args.get("task") or ""
    if not isinstance(task, str):
        return invalid("task must be text")
    task = " ".join(task.split())[:60]

    steps = Steps(websocket, pending)
    focus_timer = None
    try:
        focus_line = f"Focus session done{' on ' + task if task else ''}. Take a {_minutes(rest)} break."
        focus_timer = await steps.run(
            "set_timer", {"minutes": focus, "label": "pomodoro focus", "message": focus_line}
        )
        break_timer = await steps.run(
            "set_timer",
            {"minutes": focus + rest, "label": "pomodoro break", "message": "Break's over. Ready for the next one?"},
        )
        await steps.run(
            "show_notification",
            {
                "title": "Pomodoro started",
                "message": f"Focus until {focus_timer['ends_at']}, break until {break_timer['ends_at']}",
            },
        )
    except StepFailed as failure:
        result = steps.failed(failure)
        if failure.tool == "set_timer" and focus_timer is not None:
            try:
                await steps.run("cancel_timer", {"timer_id": focus_timer["timer_id"]})
                result["error"] += " The focus timer was cancelled again, so nothing is left running."
            except StepFailed:
                result["error"] += " The focus timer is still running."
            result["steps"] = steps.steps
        return result

    return steps.done(
        f"Pomodoro started: {focus:g} minutes of focus until {focus_timer['ends_at']}, "
        f"then a {_minutes(rest)} break until {break_timer['ends_at']}.",
        focus_timer_id=focus_timer["timer_id"],
        break_timer_id=break_timer["timer_id"],
    )


def _minutes(value: float) -> str:
    return f"{value:g} minute"
