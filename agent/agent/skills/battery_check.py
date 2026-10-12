from agent.skills.system_steps import Steps, StepFailed, battery_phrase, invalid, number


async def run(args: dict, websocket, pending: dict) -> dict:
    """Read the battery, say whether it needs attention, and put a warning
    banner on screen if it is low and not charging. One or two steps:
    get_battery_status, then show_notification only when there's a warning."""
    try:
        threshold = int(number(args, "threshold", 20, 1, 99))
    except ValueError as e:
        return invalid(str(e))

    steps = Steps(websocket, pending)
    try:
        battery = await steps.run("get_battery_status")
        text, needs_power = battery_phrase(battery, low=threshold)
        if needs_power:
            await steps.run(
                "show_notification",
                {"title": "Battery low", "message": f"{battery.get('percent')} percent and not charging. Plug in soon."},
            )
    except StepFailed as failure:
        return steps.failed(failure)
    return steps.done(text, needs_power=needs_power, percent=battery.get("percent"))
