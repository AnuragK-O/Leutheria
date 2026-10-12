from agent.skills.system_steps import Steps, StepFailed, invalid, number


async def run(args: dict, websocket, pending: dict) -> dict:
    """Mute the Mac's sound and start a focus timer. Four steps: get_volume,
    set_volume (muted), set_timer, show_notification.

    This is not macOS Focus / Do Not Disturb -- that can only be switched by
    scripting System Events, which needs a permission prompt nobody is there
    to answer. It silences sound output, which is the part that can be done
    safely and undone with one word. Because the Mac is muted, the timer's
    spoken line at the end won't be heard; its banner is what announces it,
    and the line says how to get sound back.
    """
    try:
        minutes = number(args, "minutes", 25, 0.001, 240)
    except ValueError as e:
        return invalid(str(e))

    steps = Steps(websocket, pending)
    try:
        before = await steps.run("get_volume")
        if not before.get("muted"):
            await steps.run("set_volume", {"muted": True})
        timer = await steps.run(
            "set_timer",
            {
                "minutes": minutes,
                "label": "focus",
                "message": "Focus time is over. Sound is still muted; ask me to unmute when you want it back.",
            },
        )
        await steps.run(
            "show_notification",
            {"title": "Focus mode", "message": f"Sound muted. Focus until {timer['ends_at']}."},
        )
    except StepFailed as failure:
        return steps.failed(failure)

    was = "Sound was already muted" if before.get("muted") else "Sound is muted"
    return steps.done(
        f"{was}, and a focus timer is running until {timer['ends_at']}. Notifications are not silenced.",
        timer_id=timer["timer_id"],
        previous_level=before.get("level"),
        previous_muted=before.get("muted"),
    )
