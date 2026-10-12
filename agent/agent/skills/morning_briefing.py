from agent.skills.system_steps import Steps, StepFailed, battery_phrase, invalid

LOW_DISK_GB = 15


def greeting(iso: str) -> str:
    try:
        hour = int(iso[11:13])
    except (ValueError, TypeError):
        return "Hello."
    if hour < 12:
        return "Good morning."
    return "Good afternoon." if hour < 18 else "Good evening."


async def run(args: dict, websocket, pending: dict) -> dict:
    """The date, the Mac's battery and disk, and the weather, as one spoken
    summary. All four steps are read-only.

    Weather goes last because it is the only step that needs the network: if
    it fails, the result still names it as the failed step, and everything
    gathered before it is in `steps`. With no place given and no home
    location saved the weather step isn't attempted at all -- a briefing with
    no forecast and a pointer to set one is more use than a failed briefing.
    """
    from agent.core import weather

    place = args.get("place") or ""
    if not isinstance(place, str):
        return invalid("place must be text")

    steps = Steps(websocket, pending)
    try:
        now = await steps.run("get_current_datetime")
        battery = await steps.run("get_battery_status")
        disk = await steps.run("get_disk_space")

        said = [greeting(now.get("iso", "")), f"It's {now.get('human')}.", battery_phrase(battery)[0]]
        if disk.get("free_gb") is not None and disk["free_gb"] < LOW_DISK_GB:
            said.append(f"Disk space is getting low, with {disk['free_gb']} gigabytes free.")

        if place.strip() or weather.home_location() is not None:
            report = await steps.run("get_weather", {"place": place.strip(), "days": 1} if place.strip() else {"days": 1})
            said.append(report["message"])
        else:
            said.append("I don't have a home location for the weather yet; tell me your city and I'll remember it.")
    except StepFailed as failure:
        return steps.failed(failure)
    return steps.done(" ".join(said))
