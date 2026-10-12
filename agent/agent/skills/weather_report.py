from datetime import date

from agent.skills.system_steps import Steps, StepFailed, invalid, number


def day_name(iso_date: str, index: int) -> str:
    if index == 0:
        return "Today"
    if index == 1:
        return "Tomorrow"
    try:
        return date.fromisoformat(iso_date).strftime("%A")
    except ValueError:
        return f"Day {index + 1}"


async def run(args: dict, websocket, pending: dict) -> dict:
    """A several-day outlook, read out day by day. One step (get_weather);
    the skill's job is turning the daily table into something listenable."""
    from agent.core import weather

    place = args.get("place") or ""
    if not isinstance(place, str):
        return invalid("place must be text")
    try:
        days = int(number(args, "days", 3, 2, 7))
    except ValueError as e:
        return invalid(str(e))

    steps = Steps(websocket, pending)
    try:
        weather_args = {"days": days}
        if place.strip():
            weather_args["place"] = place.strip()
        report = await steps.run("get_weather", weather_args)
    except StepFailed as failure:
        return steps.failed(failure)

    lines = [
        f"{day_name(entry.get('date', ''), i)}: {weather.describe_day(entry)}."
        for i, entry in enumerate(report.get("forecast", []))
    ]
    unit = report.get("temperature_unit", "")
    message = f"{report.get('place')}, next {len(lines)} days, in degrees {unit}. " + " ".join(lines)
    return steps.done(message, place=report.get("place"), days=len(lines))
