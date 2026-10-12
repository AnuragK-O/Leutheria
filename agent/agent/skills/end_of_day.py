from agent.skills.system_steps import Steps, StepFailed, battery_phrase

RESTART_AFTER_DAYS = 7
LOW_DISK_GB = 15


async def run(args: dict, websocket, pending: dict) -> dict:
    """An evening wrap-up that only reads: the time, timers still running,
    battery, disk, how long since a restart, and tomorrow's forecast.

    It changes nothing -- in particular it reports leftover timers rather
    than cancelling them, because one of them may be the laundry. Tomorrow's
    weather is last (the only network step) and is skipped, not failed, when
    no home location is saved; see morning_briefing for the reasoning.
    """
    from agent.core import weather

    steps = Steps(websocket, pending)
    try:
        now = await steps.run("get_current_datetime")
        timers = await steps.run("list_timers")
        battery = await steps.run("get_battery_status")
        disk = await steps.run("get_disk_space")
        system = await steps.run("get_system_info")

        said = [f"Wrapping up: it's {now.get('human')}."]
        if timers.get("count"):
            said.append(timers["message"])
        text, needs_power = battery_phrase(battery, low=40)
        said.append("Battery is low; plug in overnight." if needs_power else text)
        if disk.get("free_gb") is not None and disk["free_gb"] < LOW_DISK_GB:
            said.append(f"Disk space is getting low, with {disk['free_gb']} gigabytes free.")
        if system.get("uptime_seconds", 0) > RESTART_AFTER_DAYS * 86400:
            said.append(f"This Mac has been up for {system.get('uptime')}; a restart wouldn't hurt.")

        if weather.home_location() is not None:
            report = await steps.run("get_weather", {"days": 2})
            if len(report.get("forecast", [])) > 1:
                said.append(f"Tomorrow in {report.get('place')}: {weather.describe_day(report['forecast'][1])}.")
    except StepFailed as failure:
        return steps.failed(failure)
    return steps.done(" ".join(said))
