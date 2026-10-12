from agent.skills.system_steps import Steps, StepFailed, invalid, number


async def run(args: dict, websocket, pending: dict) -> dict:
    """Turn the volume down for the evening and set a bedtime nudge. Up to
    four steps: get_volume, set_volume, set_timer, show_notification.

    The volume is only ever lowered: if it is already at or below the target
    it is left alone, so this can't make a quiet room loud.
    """
    try:
        minutes = number(args, "minutes", 30, 0.001, 240)
        target = int(number(args, "volume", 20, 0, 100))
    except ValueError as e:
        return invalid(str(e))

    steps = Steps(websocket, pending)
    try:
        before = await steps.run("get_volume")
        if before.get("level", 0) > target:
            after = await steps.run("set_volume", {"level": target})
            volume_text = f"Volume is down to {after.get('level')} percent"
        else:
            volume_text = f"Volume was already low at {before.get('level')} percent"
        timer = await steps.run(
            "set_timer", {"minutes": minutes, "label": "wind down", "message": "Time to call it a night."}
        )
        await steps.run(
            "show_notification",
            {"title": "Winding down", "message": f"Volume lowered. I'll nudge you at {timer['ends_at']}."},
        )
    except StepFailed as failure:
        return steps.failed(failure)
    return steps.done(
        f"{volume_text}, and I'll nudge you at {timer['ends_at']}.",
        timer_id=timer["timer_id"],
        previous_level=before.get("level"),
    )
