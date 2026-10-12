from agent.core import bounded, weather


def set_home_location(place: str) -> dict:
    """Remember a place as the default for weather.

    Tagged safe although it replaces a stored value: the file holds exactly
    one small setting of Leutheria's own (never a user document), the write is
    atomic, nothing is saved unless the place was actually found, and the
    result carries the previous value so "no, put it back" is one call.
    """
    try:
        location = bounded.call_with_deadline(lambda: weather.geocode(place), weather.DEADLINE)
    except bounded.DeadlineExceeded:
        return {"ok": False, "error": "the weather service did not answer in time, so nothing was saved"}
    except weather.WeatherError as e:
        return {"ok": False, "error": f"{e}, so nothing was saved"}

    previous = weather.home_location()
    try:
        weather.save_home_location(location)
    except OSError as e:
        return {"ok": False, "error": f"could not save the home location: {e}"}

    label = weather.place_label(location)
    result = {"ok": True, "home": label, **location, "message": f"Home location set to {label}."}
    if previous:
        result["previous"] = weather.place_label(previous)
    return result
