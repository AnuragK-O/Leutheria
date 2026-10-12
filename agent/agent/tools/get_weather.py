from agent.core import weather


def get_weather(place: str = "", days: int = 1, units: str = "") -> dict:
    """Current conditions and up to a week of daily forecast from Open-Meteo.
    With no place it uses the saved home location; with neither it fails and
    says to ask, rather than guessing where the user is."""
    if isinstance(days, bool) or not isinstance(days, (int, float)) or days != days:
        return {"ok": False, "error": "days must be a number from 1 to 7"}
    days = int(days)
    if not 1 <= days <= weather.MAX_DAYS:
        return {"ok": False, "error": "days must be a number from 1 to 7"}
    if units not in ("", "metric", "imperial"):
        return {"ok": False, "error": "units must be metric or imperial"}
    if place is not None and not isinstance(place, str):
        return {"ok": False, "error": "the place must be text"}

    try:
        report = weather.lookup((place or "").strip(), days, units)
    except weather.NoPlace as e:
        return {"ok": False, "error": str(e), "needs_place": True}
    except weather.WeatherError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **report, "message": weather.summary(report)}
