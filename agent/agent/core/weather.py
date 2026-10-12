"""Open-Meteo client and the saved home location (get_weather,
set_home_location).

Open-Meteo needs no API key. Two endpoints: geocoding (a place name to
coordinates) and forecast. Where the user is comes only from what they say --
a place in the request, or a home location they asked to have remembered.
There is deliberately no IP geolocation: that would send the machine's
address to a third party to answer a question nobody asked.

Both base URLs can be replaced (module attributes, or the LEUTHERIA_WEATHER_URL
/ LEUTHERIA_GEOCODE_URL environment variables) so the checks run against a
local server with canned JSON.
"""

import json
import os
import time
from pathlib import Path

import requests

from agent.core import bounded

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

TIMEOUT = (3, 4)  # (connect, read) seconds per request
# dispatch() runs tools on the event loop, so this is how long a dead network
# can stall it at worst -- one ceiling over geocoding and forecast together.
DEADLINE = 8.0
MAX_BYTES = 200_000  # a 7-day forecast is ~3 KB; anything near this isn't one
MAX_PLACE = 100
MAX_DAYS = 7

# Local state, like preferences.json: gitignored, hand-editable, optional.
HOME_FILE = Path(__file__).resolve().parent.parent.parent / "home_location.json"

# WMO weather interpretation codes, worded to be said aloud.
WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy with frost",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "severe thunderstorms with hail",
}


class WeatherError(Exception):
    pass


def _urls() -> tuple:
    return (
        os.environ.get("LEUTHERIA_WEATHER_URL") or FORECAST_URL,
        os.environ.get("LEUTHERIA_GEOCODE_URL") or GEOCODE_URL,
    )


def _get_json(url: str, params: dict) -> dict:
    try:
        with requests.get(url, params=params, timeout=TIMEOUT, stream=True) as response:
            if response.status_code != 200:
                raise WeatherError(f"the weather service answered with an error ({response.status_code})")
            body = response.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.Timeout:
        raise WeatherError("the weather service took too long to answer") from None
    except requests.RequestException:
        raise WeatherError("could not reach the weather service; the Mac may be offline") from None
    if len(body) > MAX_BYTES:
        raise WeatherError("the weather service sent an unexpectedly large answer")
    try:
        data = json.loads(body)
    except ValueError:
        raise WeatherError("the weather service sent an answer that could not be read") from None
    if not isinstance(data, dict):
        raise WeatherError("the weather service sent an answer that could not be read")
    return data


def _clean_place(place) -> str:
    if not isinstance(place, str):
        raise WeatherError("the place must be text")
    place = " ".join(place.split())
    if not place:
        raise WeatherError("no place was given")
    if len(place) > MAX_PLACE:
        raise WeatherError("that place name is too long")
    return place


def geocode(place: str) -> dict:
    """A named place to {name, region, country, latitude, longitude}.

    Open-Meteo matches on the name alone, so "Austin, Texas" as one string
    finds nothing. The part before the first comma is searched and the rest
    is used to choose among the matches (region, country or country code).
    """
    place = _clean_place(place)
    name, _, qualifier = place.partition(",")
    qualifier = qualifier.strip().lower()
    data = _get_json(_urls()[1], {"name": name.strip(), "count": 10, "language": "en", "format": "json"})
    results = [r for r in data.get("results") or [] if isinstance(r, dict)]
    results = [r for r in results if _is_number(r.get("latitude")) and _is_number(r.get("longitude"))]
    if not results:
        raise WeatherError(f"could not find a place called {place}")
    chosen = results[0]
    if qualifier:
        for result in results:
            fields = [str(result.get(k, "")).lower() for k in ("admin1", "country", "country_code", "admin2")]
            if any(f and (qualifier == f or qualifier in f) for f in fields):
                chosen = result
                break
    return {
        "name": str(chosen.get("name") or name.strip())[:MAX_PLACE],
        "region": str(chosen.get("admin1") or "")[:MAX_PLACE],
        "country": str(chosen.get("country") or "")[:MAX_PLACE],
        "latitude": round(float(chosen["latitude"]), 4),
        "longitude": round(float(chosen["longitude"]), 4),
    }


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def place_label(location: dict) -> str:
    extra = location.get("region") or location.get("country")
    if extra and extra != location["name"]:
        return f"{location['name']}, {extra}"
    return location["name"]


def default_units() -> str:
    """'imperial' where the Mac's region uses it, else 'metric'."""
    try:
        from Foundation import NSLocale

        region = str(NSLocale.currentLocale().localeIdentifier()).split("@")[0].split("_")[-1].upper()
    except Exception:
        return "metric"
    return "imperial" if region in {"US", "LR", "MM"} else "metric"


def _num(value):
    return int(round(value)) if _is_number(value) else None


def _at(series, index):
    return series[index] if isinstance(series, list) and index < len(series) else None


def forecast(location: dict, days: int = 1, units: str = "") -> dict:
    """Current conditions plus `days` daily entries (today first), small and
    already rounded -- what goes back to the model, not the raw payload."""
    units = units if units in ("metric", "imperial") else default_units()
    params = {
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "auto",
        "forecast_days": max(1, min(MAX_DAYS, days)),
    }
    if units == "imperial":
        params.update(temperature_unit="fahrenheit", wind_speed_unit="mph")
    data = _get_json(_urls()[0], params)

    current = data.get("current")
    daily = data.get("daily")
    if not isinstance(current, dict) or not isinstance(daily, dict) or _num(current.get("temperature_2m")) is None:
        raise WeatherError("the weather service sent an answer with no forecast in it")

    dates = daily.get("time") if isinstance(daily.get("time"), list) else []
    entries = []
    for i, date in enumerate(dates[:MAX_DAYS]):
        entries.append(
            {
                "date": str(date)[:10],
                "condition": WMO.get(_at(daily.get("weather_code"), i), "unsettled"),
                "high": _num(_at(daily.get("temperature_2m_max"), i)),
                "low": _num(_at(daily.get("temperature_2m_min"), i)),
                "rain_chance_percent": _num(_at(daily.get("precipitation_probability_max"), i)),
            }
        )
    return {
        "place": place_label(location),
        "units": units,
        "temperature_unit": "fahrenheit" if units == "imperial" else "celsius",
        "wind_unit": "mph" if units == "imperial" else "km/h",
        "current": {
            "temperature": _num(current.get("temperature_2m")),
            "feels_like": _num(current.get("apparent_temperature")),
            "condition": WMO.get(current.get("weather_code"), "unsettled"),
            "humidity_percent": _num(current.get("relative_humidity_2m")),
            "wind_speed": _num(current.get("wind_speed_10m")),
        },
        "forecast": entries,
    }


def describe_day(entry: dict) -> str:
    """'partly cloudy, high 80, low 61, 20 percent chance of rain'."""
    parts = [entry["condition"]]
    if entry.get("high") is not None:
        parts.append(f"high {entry['high']}")
    if entry.get("low") is not None:
        parts.append(f"low {entry['low']}")
    if entry.get("rain_chance_percent"):
        parts.append(f"{entry['rain_chance_percent']} percent chance of rain")
    return ", ".join(parts)


def summary(report: dict) -> str:
    now = report["current"]
    text = f"In {report['place']} it's {now['temperature']} degrees and {now['condition']}"
    if now.get("feels_like") is not None and abs(now["feels_like"] - now["temperature"]) >= 3:
        text += f", feels like {now['feels_like']}"
    text += "."
    if report["forecast"]:
        text += f" Today: {describe_day(report['forecast'][0])}."
    return text


def lookup(place: str = "", days: int = 1, units: str = "") -> dict:
    """The whole get_weather call under one deadline. Raises WeatherError."""
    started = time.monotonic()

    def work():
        location = geocode(place) if place else home_location()
        if location is None:
            raise NoPlace()
        return forecast(location, days, units)

    try:
        return bounded.call_with_deadline(work, DEADLINE)
    except bounded.DeadlineExceeded:
        waited = round(time.monotonic() - started)
        raise WeatherError(f"the weather service did not answer within {waited} seconds") from None


class NoPlace(WeatherError):
    def __init__(self):
        super().__init__(
            "No place was given and no home location is saved. Ask which city they mean; "
            "set_home_location can remember it for next time."
        )


# --- home location -----------------------------------------------------------


def home_location():
    """The saved home location, or None. A missing, unreadable or malformed
    file is "none saved", never an error -- it's optional local state."""
    try:
        data = json.loads(HOME_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not data["name"].strip():
        return None
    if not _is_number(data.get("latitude")) or not _is_number(data.get("longitude")):
        return None
    if not -90 <= data["latitude"] <= 90 or not -180 <= data["longitude"] <= 180:
        return None
    return {
        "name": data["name"][:MAX_PLACE],
        "region": str(data.get("region") or "")[:MAX_PLACE],
        "country": str(data.get("country") or "")[:MAX_PLACE],
        "latitude": data["latitude"],
        "longitude": data["longitude"],
    }


def save_home_location(location: dict) -> None:
    tmp = HOME_FILE.with_name(f".{HOME_FILE.name}.tmp")
    tmp.write_text(json.dumps(location, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, HOME_FILE)
