"""Check of the system/productivity tools and skills (week 5).

    cd agent
    ./.venv/bin/python scripts/skills_check_system.py

What it is careful about, since someone is usually sitting at this Mac:

  - volume      the setter is a fake that models the system; the real volume
                is read, written back to the value it already has once (to
                prove the real AppleScript is accepted), and compared at the
                end. It never moves.
  - media keys  the sender is a recorder. The real events are built, to prove
                they can be, and never posted.
  - banners     show_notification is replaced before anything runs.
  - speech      tts.speak is replaced (and the player silenced as well).
  - windows     open_file / reveal_in_finder / open_app record their argv.
  - network     weather runs against a local http.server with canned
                Open-Meteo JSON. One real request per network tool, skipped
                cleanly when offline.
  - state       the event log, preferences and the home location file are
                redirected into a temp dir; nothing touches port 8765 (the
                server part binds an ephemeral port) or the microphone.

Skills are driven end to end through the real dispatch() with a fake
websocket that answers confirmations. Prints one line per check and exits
non-zero on the first failure.
"""

import asyncio
import inspect
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

TMP = Path(tempfile.mkdtemp(prefix="leutheria_system_check_")).resolve()
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
for name in ("LEUTHERIA_WEATHER_URL", "LEUTHERIA_GEOCODE_URL"):
    os.environ.pop(name, None)

# Redirect local state before anything imports the registries.
from agent.core import logging_util  # noqa: E402

logging_util.LOG_DIR = TMP / "logs"
logging_util.LOG_FILE = logging_util.LOG_DIR / "events.jsonl"

from agent.core import preferences  # noqa: E402

preferences.PREFS_FILE = TMP / "preferences.json"
preferences._overrides.clear()

from agent.core import bounded, dispatcher, timers, tts, weather  # noqa: E402
from agent.core.dispatcher import dispatch  # noqa: E402
from agent.skills import quick_timer  # noqa: E402
from agent.skills.registry import SKILLS  # noqa: E402
from agent.tools import get_network_status as network_tool  # noqa: E402
from agent.tools import get_system_info as system_tool  # noqa: E402
from agent.tools import get_volume as volume_tool  # noqa: E402
from agent.tools import media_control as media_tool  # noqa: E402
from agent.tools import open_file as open_tool  # noqa: E402
from agent.tools import reveal_in_finder as reveal_tool  # noqa: E402
from agent.tools.registry import TOOLS  # noqa: E402

REAL_HOME_FILE = weather.HOME_FILE
REAL_HOME_STATE = REAL_HOME_FILE.stat().st_mtime_ns if REAL_HOME_FILE.exists() else None
weather.HOME_FILE = TMP / "home_location.json"

SYSTEM_TOOLS = [
    "get_volume", "set_volume", "media_control", "get_system_info", "get_network_status", "get_weather",
    "set_home_location", "set_timer", "list_timers", "cancel_timer", "open_file", "reveal_in_finder",
]
SYSTEM_SKILLS = [
    "morning_briefing", "system_health_check", "weather_report", "quick_timer", "pomodoro", "focus_mode",
    "meeting_prep", "end_of_day", "wind_down", "battery_check",
]

passed = 0


def check(condition, label):
    global passed
    if not condition:
        print(f"FAIL  {label}")
        sys.exit(1)
    passed += 1
    print(f"ok    {label}")


def skip(label):
    print(f"skip  {label}")


def events(kind):
    if not logging_util.LOG_FILE.exists():
        return []
    records = [json.loads(line) for line in logging_util.LOG_FILE.read_text().splitlines()]
    return [r for r in records if r["type"] == kind]


# --- stubs installed before anything can reach the user -----------------------

notifications = []
notify_mode = {"mode": "ok"}
REAL_NOTIFY = TOOLS["show_notification"]["fn"]


def fake_notify(title: str, message: str) -> dict:
    notifications.append({"title": title, "message": message})
    if notify_mode["mode"] == "raise":
        raise RuntimeError("banner exploded")
    if notify_mode["mode"] == "fail":
        return {"ok": False, "error": "notifications are off"}
    return {"ok": True, "message": "notification shown"}


TOOLS["show_notification"]["fn"] = fake_notify

spoken = []


async def fake_speak(websocket, text):
    spoken.append({"websocket": websocket, "text": text})


tts.speak = fake_speak
dispatcher.speak = fake_speak  # dispatcher imported the name; confirmations speak through it
tts.set_player(lambda audio, rate: None)  # belt and braces: nothing can reach the speakers

media_posts = []
REAL_MEDIA_POST = media_tool._post_media_key
media_tool._post_media_key = lambda key_code: media_posts.append(key_code)

launches = []


def fake_launch(argv, timeout=3.0):
    launches.append(list(argv))
    return 0, "", ""


open_tool._launch = fake_launch
reveal_tool._launch = fake_launch

opened_apps = []


def fake_open_app(name: str) -> dict:
    opened_apps.append(name)
    return {"ok": True, "message": f"opened {name}"}


TOOLS["open_app"]["fn"] = fake_open_app


class FakeVolume:
    """Stands in for osascript: models the two values the tools touch and
    refuses any script that isn't one of the two exact forms they may send."""

    SET = re.compile(r"set volume output volume (\d{1,3}) output muted (true|false)")

    def __init__(self, level=44, muted=False):
        self.level, self.muted, self.scripts = level, muted, []
        self.no_control = False
        self.fail = False

    def __call__(self, script: str) -> tuple:
        self.scripts.append(script)
        if self.fail:
            return 1, "", "execution error: boom"
        if script == "get volume settings":
            level = "missing value" if self.no_control else self.level
            return 0, f"output volume:{level}, input volume:20, alert volume:100, output muted:{str(self.muted).lower()}\n", ""
        match = self.SET.fullmatch(script)
        if not match:
            raise AssertionError(f"unexpected AppleScript: {script!r}")
        self.level, self.muted = int(match.group(1)), match.group(2) == "true"
        return 0, "", ""


class FakeWebSocket:
    """A UI connection: records what it is sent and answers confirmations."""

    def __init__(self, pending: dict, approve: bool = True):
        self.pending, self.approve, self.sent = pending, approve, []

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message.get("type") == "confirmation_required":
            future = self.pending[message["id"]]
            asyncio.get_running_loop().call_soon(
                future.set_result, {"approved": self.approve, "remember": None}
            )


def run_tool(name, **args):
    """A tool through the real dispatch(), outside any event loop."""
    return asyncio.run(dispatch(name, args))


def step_tools(result):
    return [s["tool"] for s in result.get("steps", [])]


print(f"temp dir: {TMP}\n")

# --- 0. registry shape ---------------------------------------------------------
print("# registry")
for name in SYSTEM_TOOLS:
    tool = TOOLS.get(name)
    check(tool is not None and {"fn", "safety", "description", "input_schema"} <= set(tool), f"{name}: registered with all four fields")
    check(tool["safety"] == "safe", f"{name}: tier is safe")
    props = set(tool["input_schema"]["properties"])
    params = set(inspect.signature(tool["fn"]).parameters)
    check(props == params, f"{name}: schema properties match the function's parameters ({sorted(props ^ params)})")
    check(set(tool["input_schema"]["required"]) <= props, f"{name}: required is a subset of properties")
for name in SYSTEM_SKILLS:
    skill = SKILLS.get(name)
    check(skill is not None and {"fn", "description", "input_schema"} <= set(skill), f"{name}: skill registered")
    check(name not in TOOLS, f"{name}: skill name doesn't shadow a tool")
    check(inspect.iscoroutinefunction(skill["fn"]), f"{name}: run is async (args, websocket, pending)")
    source = Path(inspect.getsourcefile(skill["fn"])).read_text()
    check("agent.tools" not in source, f"{name}: module imports no tool function (every step is a dispatch)")
    check(re.search(r" not | cannot |instead", skill["description"]), f"{name}: description says what it is not for")
for name in SYSTEM_TOOLS + SYSTEM_SKILLS:
    entry = TOOLS.get(name) or SKILLS[name]
    texts = [entry["description"]] + [p.get("description", "") for p in entry["input_schema"]["properties"].values()]
    clean = all(not re.search(r"[`*#\\\n]|\s{2,}", text) for text in texts)
    check(clean, f"{name}: description has no markdown, escapes or stray whitespace")

# --- 1. volume -----------------------------------------------------------------
print("\n# volume")
REAL_OSASCRIPT = volume_tool._osascript
real_before = volume_tool.read_volume()
if "error" in real_before:
    skip(f"real volume read ({real_before['error']})")
else:
    check(0 <= real_before["level"] <= 100 and isinstance(real_before["muted"], bool),
          f"real get_volume parses (level {real_before['level']}, muted {real_before['muted']})")
    # The one real write: the value it already has, with the mute state it
    # already has. Proves osascript accepts the exact script set_volume builds.
    script = f"set volume output volume {real_before['level']} output muted {'true' if real_before['muted'] else 'false'}"
    code, _, err = REAL_OSASCRIPT(script)
    check(code == 0, f"real osascript accepts set_volume's script as a no-op write ({err})")
    check(volume_tool.read_volume() == real_before, "real volume unchanged by the no-op write")

fake_volume = FakeVolume()
volume_tool._osascript = fake_volume

result = run_tool("get_volume")
check(result == {"ok": True, "level": 44, "muted": False, "message": "Volume is at 44 percent."}, "get_volume reads level and mute")
result = run_tool("set_volume", level=30)
check(result["ok"] and result["level"] == 30 and result["previous_level"] == 44 and fake_volume.level == 30,
      "set_volume level: sets it and reports the previous level")
check(fake_volume.scripts[-2] == "set volume output volume 30 output muted false", "set_volume writes the mute state explicitly")
result = run_tool("set_volume", change=-50)
check(result["level"] == 0 and fake_volume.level == 0, "set_volume change clamps at 0")
result = run_tool("set_volume", change=100)
check(result["level"] == 100, "set_volume change clamps at 100")
result = run_tool("set_volume", level=40.0)
check(result["ok"] and result["level"] == 40, "set_volume accepts a whole number sent as a float")
result = run_tool("set_volume", muted=True)
check(result["muted"] and result["level"] == 40 and not result["previous_muted"], "mute keeps the level")
result = run_tool("set_volume", level=60)
check(result["muted"] and result["level"] == 60 and "note" in result, "a level change does not unmute, and says so")
result = run_tool("set_volume", level=50, muted=False)
check(not result["muted"] and result["level"] == 50 and "note" not in result, "level and unmute in one call")
before_scripts = len(fake_volume.scripts)
for bad, label in [({}, "no arguments"), ({"level": 10, "change": 5}, "level and change together"),
                   ({"level": 101}, "level 101"), ({"level": -1}, "level -1"), ({"level": True}, "level true"),
                   ({"level": "50"}, "level as text"), ({"change": 500}, "change 500"), ({"muted": "yes"}, "muted as text"),
                   ({"level": float("nan")}, "level NaN")]:
    result = run_tool("set_volume", **bad)
    check(result["ok"] is False and fake_volume.level == 50, f"set_volume rejects {label} ({result.get('error')})")
check(len(fake_volume.scripts) == before_scripts, "a rejected set_volume never reaches osascript")
check(run_tool("set_volume", volume=5)["ok"] is False, "an unknown argument is an error, not a crash")
fake_volume.no_control = True
check(run_tool("get_volume")["ok"] is False and run_tool("set_volume", level=10)["ok"] is False,
      "an output device with no software volume is a clean error")
fake_volume.no_control = False
fake_volume.fail = True
check(run_tool("get_volume")["ok"] is False and run_tool("set_volume", level=10)["ok"] is False, "an osascript failure is a clean error")
fake_volume.fail = False
check(fake_volume.level == 50 and not fake_volume.muted, "the failures above changed nothing")

# --- 2. media keys -------------------------------------------------------------
print("\n# media_control")
media_tool._can_post = lambda: True
for action, code in [("play_pause", 16), ("next", 17), ("previous", 18)]:
    media_posts.clear()
    result = run_tool("media_control", action=action)
    check(result["ok"] and media_posts == [code], f"{action} would send media key {code}, once")
check("cannot tell" in run_tool("media_control", action="play_pause")["note"], "the result admits it can't see what is playing")
media_posts.clear()
for bad in ["stop", "volume_up", "", "16", None, 16]:
    check(run_tool("media_control", action=bad)["ok"] is False, f"action {bad!r} rejected")
check(media_posts == [], "a rejected action sends nothing")
media_tool._can_post = lambda: False
result = run_tool("media_control", action="next")
check(result["ok"] is False and "Accessibility" in result["error"] and media_posts == [],
      "without Accessibility permission it fails loudly and sends nothing")
media_tool._can_post = lambda: True
built = media_tool._media_key_events(16)
check(len(built) == 2 and [e.subtype() for e in built] == [8, 8]
      and [e.data1() for e in built] == [(16 << 16) | (0xA << 8), (16 << 16) | (0xB << 8)]
      and all(e.CGEvent() is not None for e in built),
      "the real key-down/key-up events build correctly (built, never posted)")
check(media_tool._post_media_key is not REAL_MEDIA_POST, "the real media-key sender was never installed during this run")

# --- 3. system info ------------------------------------------------------------
print("\n# get_system_info")
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    10000.
Pages active:                                 400000.
Pages inactive:                               300000.
Pages wired down:                             100000.
Pages purgeable:                               50000.
"Translation faults":                      407247142.
Anonymous pages:                              450000.
Pages occupied by compressor:                  50000.
"""
canned = {
    ("sysctl", "-n", "hw.memsize"): "17179869184\n",
    ("sysctl", "-n", "kern.boottime"): f"{{ sec = {int(time.time()) - 90061}, usec = 0 }} Mon\n",
    ("sysctl", "-n", "hw.model"): "Mac99,1\n",
    ("sysctl", "-n", "machdep.cpu.brand_string"): "Apple M9\n",
    ("sysctl", "-n", "kern.memorystatus_vm_pressure_level"): "2\n",
    ("vm_stat",): VM_STAT,
}
seen_argv = []


def canned_run(argv, timeout=3.0):
    seen_argv.append(tuple(argv))
    if tuple(argv) in canned:
        return 0, canned[tuple(argv)], ""
    return 1, "", "not canned"


REAL_RUN = bounded.run
system_tool._run = canned_run
info = run_tool("get_system_info")
# used = (450000 - 50000 + 100000 + 50000) pages * 16384 = 8.39 GiB of 16
check(info["ok"] and info["memory"] == {"total_gb": 16.0, "used_gb": 8.4, "available_gb": 7.6, "used_percent": 52, "pressure": "warning"},
      f"memory is computed from vm_stat the way Activity Monitor counts it ({info.get('memory')})")
check(info["uptime"] == "1 day 1 hour" and abs(info["uptime_seconds"] - 90061) < 5, f"uptime from kern.boottime ({info['uptime']})")
check(info["model"] == "Mac99,1" and info["chip"] == "Apple M9" and info["cpu_cores"] >= 1, "model, chip and cores reported")
check(info["message"].startswith("CPU load is about") and "memory is 52 percent used" in info["message"], f"spoken summary ({info['message']})")
canned.clear()
info = run_tool("get_system_info")
check(info["ok"] and "memory" not in info and "uptime" not in info, "missing sysctl/vm_stat output degrades to fewer fields, not an error")
system_tool._run = REAL_RUN
info = run_tool("get_system_info")
check(info["ok"] and 0 < info["memory"]["used_percent"] <= 100 and info["uptime_seconds"] > 0 and info["macos_version"],
      f"real call: {info['message']}")
check(len(json.dumps(info)) < 1500, "result is small")

# --- 4. network ----------------------------------------------------------------
print("\n# get_network_status")
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(5)
closed = socket.socket()
closed.bind(("127.0.0.1", 0))
closed_port = closed.getsockname()[1]
closed.close()

ROUTE = "   route to: default\ndestination: default\n    gateway: 10.0.0.1\n  interface: en0\n      flags: <UP>\n"
PORTS = "\nHardware Port: Ethernet\nDevice: en7\nEthernet Address: aa\n\nHardware Port: Wi-Fi\nDevice: en0\nEthernet Address: bb\n"
net = {("route", "-n", "get", "default"): (0, ROUTE, ""), ("networksetup", "-listallhardwareports"): (0, PORTS, ""),
       ("ipconfig", "getifaddr", "en0"): (0, "10.0.0.42\n", "")}
network_tool._run = lambda argv, timeout=3.0: net.get(tuple(argv), (1, "", "not canned"))
REAL_PROBE = network_tool.PROBE
network_tool.PROBE = listener.getsockname()
result = run_tool("get_network_status")
check(result == {"ok": True, "connected": True, "internet": True, "interface": "en0", "kind": "Wi-Fi",
                 "message": "Connected over Wi-Fi, and the internet is reachable.", "ip_address": "10.0.0.42", "router": "10.0.0.1"},
      f"connected and reachable ({result})")
network_tool.PROBE = ("127.0.0.1", closed_port)
result = run_tool("get_network_status")
check(result["connected"] and result["internet"] is False and "not reachable" in result["message"], "connected but the internet probe fails")
net[("route", "-n", "get", "default")] = (0, ROUTE.replace("en0", "utun4"), "")
check(run_tool("get_network_status")["kind"] == "VPN", "a utun default route is named as a VPN")
net[("route", "-n", "get", "default")] = (0, ROUTE.replace("en0", "en0; rm -rf"), "")
result = run_tool("get_network_status")
check(result["ok"] is False, "a malformed interface name is refused before it reaches ipconfig")
net[("route", "-n", "get", "default")] = (1, "", "route: writing to routing socket: not in table")
result = run_tool("get_network_status")
check(result == {"ok": True, "connected": False, "internet": False, "message": "This Mac is not connected to any network."},
      "no default route: reports not connected, cleanly")
listener.close()
network_tool._run = REAL_RUN
network_tool.PROBE = REAL_PROBE
started = time.monotonic()
result = run_tool("get_network_status")
check(result["ok"] and isinstance(result["connected"], bool) and time.monotonic() - started < 15,
      f"real call (works online or offline): {result['message']}")
ONLINE = bool(result.get("internet"))


# --- 5. weather ----------------------------------------------------------------
print("\n# get_weather")
PLACES = {
    "austin": [
        {"name": "Austin", "admin1": "Minnesota", "country": "United States", "country_code": "US", "latitude": 43.66663, "longitude": -92.97464},
        {"name": "Austin", "admin1": "Texas", "country": "United States", "country_code": "US", "latitude": 30.26715, "longitude": -97.74306},
    ],
    "paris": [{"name": "Paris", "admin1": "Ile-de-France", "country": "France", "country_code": "FR", "latitude": 48.85341, "longitude": 2.3488}],
    "broken": [{"name": "Broken", "latitude": "north", "longitude": None}],
}


class OpenMeteo(BaseHTTPRequestHandler):
    mode = "ok"
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        OpenMeteo.seen.append((url.path, query))
        mode = OpenMeteo.mode
        if mode == "slow":
            time.sleep(2.5)
        if mode == "error500":
            return self._send(500, b'{"error":true,"reason":"boom"}')
        if mode == "garbage":
            return self._send(200, b"<html>captive portal</html>")
        if mode == "list":
            return self._send(200, b"[1, 2, 3]")
        if mode == "huge":
            return self._send(200, json.dumps({"pad": "x" * 300_000}).encode())
        if url.path == "/v1/search":
            results = PLACES.get(query.get("name", "").lower())
            body = {"generationtime_ms": 0.5}
            if results:
                body["results"] = results
            return self._send(200, json.dumps(body).encode())
        if mode == "nocurrent":
            return self._send(200, b'{"daily": {}}')
        days = int(query.get("forecast_days", 1))
        imperial = query.get("temperature_unit") == "fahrenheit"
        body = {
            "current": {"temperature_2m": 71.6 if imperial else 22.0, "apparent_temperature": 75.4 if imperial else 24.1,
                        "relative_humidity_2m": 40, "weather_code": 2, "wind_speed_10m": 8.3},
            "daily": {
                "time": ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"][:days],
                "weather_code": [2, 61, 0, 95, 3, 71, 1][:days],
                "temperature_2m_max": ([80.4, 70.2, 75.0, 77.0, 78.0, 60.0, 65.0] if imperial else [26.9, 21.2, 23.9, 25, 25.6, 15.6, 18.3])[:days],
                "temperature_2m_min": ([61.2, 55.0, 58.0, 60.0, 61.0, 40.0, 45.0] if imperial else [16.2, 12.8, 14.4, 15.6, 16.1, 4.4, 7.2])[:days],
                "precipitation_probability_max": [20, 70, 0, 90, 10, None, 5][:days],
            },
        }
        return self._send(200, json.dumps(body).encode())

    def _send(self, status, body):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # the client gave up (the deadline test)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


http = QuietServer(("127.0.0.1", 0), OpenMeteo)
threading.Thread(target=http.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{http.server_address[1]}"
REAL_URLS = (weather.FORECAST_URL, weather.GEOCODE_URL)
weather.FORECAST_URL, weather.GEOCODE_URL = f"{BASE}/v1/forecast", f"{BASE}/v1/search"

result = run_tool("get_weather", place="Austin, Texas", units="imperial")
check(result["ok"] and result["place"] == "Austin, Texas", f"'Austin, Texas' picks the Texas match, not the first one ({result.get('place')})")
search, forecast_call = OpenMeteo.seen[-2], OpenMeteo.seen[-1]
check(search[0] == "/v1/search" and search[1]["name"] == "Austin", "geocoding is asked for the name before the comma")
check(abs(float(forecast_call[1]["latitude"]) - 30.26715) < 0.0002 and abs(float(forecast_call[1]["longitude"]) + 97.74306) < 0.0002 and forecast_call[1]["temperature_unit"] == "fahrenheit"
      and forecast_call[1]["wind_speed_unit"] == "mph" and forecast_call[1]["forecast_days"] == "1",
      "forecast request carries the coordinates, units and day count")
check(result["current"] == {"temperature": 72, "feels_like": 75, "condition": "partly cloudy", "humidity_percent": 40, "wind_speed": 8},
      "current conditions are rounded and worded")
check(result["forecast"] == [{"date": "2026-10-12", "condition": "partly cloudy", "high": 80, "low": 61, "rain_chance_percent": 20}],
      "one daily entry by default")
check(result["message"] == "In Austin, Texas it's 72 degrees and partly cloudy, feels like 75. "
      "Today: partly cloudy, high 80, low 61, 20 percent chance of rain.", f"spoken summary ({result['message']})")
check(len(json.dumps(result)) < 800, "result is small")
result = run_tool("get_weather", place="Paris", days=7, units="metric")
check(result["ok"] and len(result["forecast"]) == 7 and result["temperature_unit"] == "celsius"
      and "temperature_unit" not in OpenMeteo.seen[-1][1], "seven days, metric (the API default units)")
check(result["forecast"][5]["rain_chance_percent"] is None and result["forecast"][3]["condition"] == "thunderstorms",
      "a missing value stays missing; WMO codes are worded")
result = run_tool("get_weather", place="Paris")
check(result["ok"] and result["units"] == weather.default_units(), f"units default to the Mac's region ({result['units']})")
calls = len(OpenMeteo.seen)
for bad, label in [({"place": "Paris", "days": 0}, "days 0"), ({"place": "Paris", "days": 8}, "days 8"),
                   ({"place": "Paris", "days": True}, "days true"), ({"place": "Paris", "units": "kelvin"}, "units kelvin"),
                   ({"place": 12}, "a numeric place"), ({"place": "x" * 101}, "a 101-character place")]:
    result = run_tool("get_weather", **bad)
    check(result["ok"] is False, f"get_weather rejects {label} ({result.get('error')})")
check(len(OpenMeteo.seen) == calls, "none of those reached the network")
result = run_tool("get_weather", place="Nowhereville")
check(result["ok"] is False and "could not find a place called Nowhereville" in result["error"], "unknown place is a clean error")
check(run_tool("get_weather", place="Broken")["ok"] is False, "a geocoding result without usable coordinates is rejected")
result = run_tool("get_weather")
check(result["ok"] is False and result.get("needs_place") and "Ask which city" in result["error"],
      "no place and no home location: fails and says to ask (no guessing, no IP lookup)")
for mode, expect in [("error500", "answered with an error (500)"), ("garbage", "could not be read"), ("list", "could not be read"),
                     ("huge", "unexpectedly large"), ("nocurrent", "no forecast in it")]:
    OpenMeteo.mode = mode
    result = run_tool("get_weather", place="Paris")
    check(result["ok"] is False and expect in result["error"], f"server {mode}: {result.get('error')}")
OpenMeteo.mode = "slow"
old_timeout, old_deadline = weather.TIMEOUT, weather.DEADLINE
weather.TIMEOUT, weather.DEADLINE = (1, 0.5), 5.0
started = time.monotonic()
result = run_tool("get_weather", place="Paris")
check(result["ok"] is False and "too long" in result["error"] and time.monotonic() - started < 2.4, f"a slow server hits the read timeout ({result['error']})")
weather.TIMEOUT, weather.DEADLINE = (5, 30), 1.0
started = time.monotonic()
result = run_tool("get_weather", place="Paris")
check(result["ok"] is False and "did not answer within" in result["error"] and time.monotonic() - started < 2.0,
      f"the overall deadline holds even when the per-read timeout would not ({result['error']})")
weather.TIMEOUT, weather.DEADLINE = old_timeout, old_deadline
OpenMeteo.mode = "ok"
weather.FORECAST_URL = weather.GEOCODE_URL = f"http://127.0.0.1:{closed_port}/v1/x"
started = time.monotonic()
result = run_tool("get_weather", place="Paris")
check(result["ok"] is False and "may be offline" in result["error"] and time.monotonic() - started < 5, f"offline is a clean error ({result['error']})")
weather.FORECAST_URL, weather.GEOCODE_URL = f"{BASE}/v1/forecast", f"{BASE}/v1/search"
os.environ["LEUTHERIA_WEATHER_URL"] = f"http://127.0.0.1:{closed_port}/v1/forecast"
check(run_tool("get_weather", place="Paris")["ok"] is False, "LEUTHERIA_WEATHER_URL overrides the base URL")
del os.environ["LEUTHERIA_WEATHER_URL"]

# --- 6. home location ----------------------------------------------------------
print("\n# set_home_location")
check(weather.home_location() is None and not weather.HOME_FILE.exists(), "no home location to begin with")
result = run_tool("set_home_location", place="Nowhereville")
check(result["ok"] is False and "nothing was saved" in result["error"] and not weather.HOME_FILE.exists(), "an unknown place saves nothing")
result = run_tool("set_home_location", place="Paris")
check(result["ok"] and result["home"] == "Paris, Ile-de-France" and "previous" not in result, "saves a found place")
saved = json.loads(weather.HOME_FILE.read_text())
check(saved == {"name": "Paris", "region": "Ile-de-France", "country": "France", "latitude": 48.8534, "longitude": 2.3488},
      "the file holds only the place: name, region, country, coordinates")
calls = len(OpenMeteo.seen)
result = run_tool("get_weather")
check(result["ok"] and result["place"] == "Paris, Ile-de-France" and len(OpenMeteo.seen) == calls + 1
      and OpenMeteo.seen[-1][0] == "/v1/forecast", "get_weather with no place uses home, with no geocoding call")
result = run_tool("set_home_location", place="Austin, Texas")
check(result["ok"] and result["previous"] == "Paris, Ile-de-France" and result["home"] == "Austin, Texas", "replacing reports the previous home")
check(not list(TMP.glob(".home_location*")), "the atomic write leaves no temp file behind")
for junk in ["{not json", "[]", '{"name": "X"}', '{"name": "X", "latitude": 999, "longitude": 0}', '{"name": "", "latitude": 1, "longitude": 1}']:
    weather.HOME_FILE.write_text(junk)
    check(weather.home_location() is None, f"a malformed home file is 'none saved', not a crash ({junk[:24]})")
weather.HOME_FILE.unlink()

if ONLINE:
    weather.FORECAST_URL, weather.GEOCODE_URL = REAL_URLS
    result = run_tool("get_weather", place="Berlin", units="metric")
    if result["ok"]:
        check(result["place"].startswith("Berlin") and -60 < result["current"]["temperature"] < 60 and len(result["forecast"]) == 1,
              f"real Open-Meteo call: {result['message']}")
    else:
        skip(f"real Open-Meteo call ({result['error']})")
    weather.FORECAST_URL, weather.GEOCODE_URL = f"{BASE}/v1/forecast", f"{BASE}/v1/search"
else:
    skip("real Open-Meteo call (offline)")

# --- 7. open_file / reveal_in_finder ---------------------------------------------
print("\n# open_file / reveal_in_finder")
FILES = TMP / "files"
FILES.mkdir()
for name in ["notes.txt", "paper.pdf", "photo.png", "song.mp3", "run.command", "build.sh", "tool.py", "app.jar", "mystery.xyzunknown",
             "noext", "profile.mobileconfig", "setup.pkg", "disk.dmg", "link.webloc", "My File (final).txt"]:
    (FILES / name).write_bytes(b"")
(FILES / "folder").mkdir()
(FILES / "Fake.app").mkdir()
shutil.copy("/bin/ls", FILES / "program")
os.chmod(FILES / "run.command", 0o755)
os.symlink(FILES / "run.command", FILES / "innocent.txt.lnk")
os.symlink(FILES / "run.command", FILES / "alias.txt")

launches.clear()
for name in ["notes.txt", "paper.pdf", "photo.png", "song.mp3", "My File (final).txt"]:
    result = run_tool("open_file", path=str(FILES / name))
    if result["ok"]:
        argv = launches[-1]
        check(argv[:2] == ["open", "-a"] and argv[2].endswith(".app") and argv[3] == str(FILES / name) and len(argv) == 4,
              f"{name}: opened with its checked handler, named explicitly ({result['app']})")
    else:
        check("no app is set up" in result["error"], f"{name}: no default app on this Mac ({result['error']})")
result = run_tool("open_file", path=str(FILES / "folder"))
check(result["ok"] and launches[-1] == ["open", str(FILES / "folder")] and result["app"] == "Finder", "a plain folder opens in Finder")
count = len(launches)
for name, why in [("run.command", "script"), ("build.sh", "script"), ("tool.py", "script"), ("app.jar", "program"),
                  ("program", "program"), ("Fake.app", "application"), ("mystery.xyzunknown", "not a kind"),
                  ("noext", "not a kind"), ("profile.mobileconfig", "script or program"), ("setup.pkg", "not a kind"),
                  ("disk.dmg", "not a kind"), ("link.webloc", "not a kind"), ("alias.txt", "script"), ("innocent.txt.lnk", "script")]:
    result = run_tool("open_file", path=str(FILES / name))
    check(result["ok"] is False and why in result["error"] and "reveal_in_finder" in result["error"],
          f"{name}: refused ({result['error'][:70]})")
check(len(launches) == count, "nothing refused was ever handed to `open`")
open_tool.EXECUTING_HANDLERS.add("textedit")
open_tool.EXECUTING_HANDLERS.add("xcode")
result = run_tool("open_file", path=str(FILES / "notes.txt"))
check(result["ok"] is False and ("would run it" in result["error"] or "no app is set up" in result["error"]),
      "a content file whose handler is an executing app is refused (per-file 'open with Terminal')")
open_tool.EXECUTING_HANDLERS.discard("textedit")
open_tool.EXECUTING_HANDLERS.discard("xcode")
for bad in ["", "   ", str(FILES / "missing.txt"), 12, None]:
    check(run_tool("open_file", path=bad)["ok"] is False and run_tool("reveal_in_finder", path=bad)["ok"] is False,
          f"path {bad!r}: clean error from both")
check(len(launches) == count, "bad paths launch nothing")
for name in ["notes.txt", "run.command", "program", "Fake.app", "folder"]:
    result = run_tool("reveal_in_finder", path=str(FILES / name))
    check(result["ok"] and launches[-1] == ["open", "-R", str(FILES / name)], f"reveal_in_finder {name}: open -R, never opens it")
real_home = os.environ.get("HOME")
os.environ["HOME"] = str(FILES)
result = run_tool("reveal_in_finder", path="~/notes.txt")
os.environ["HOME"] = real_home
check(result["ok"] and launches[-1][-1] == str(FILES / "notes.txt"), "~ is expanded before the path is used")
result = run_tool("reveal_in_finder", path=str(FILES / "-R"))
check(result["ok"] is False, "a missing path that looks like a flag is just a missing path")


# --- 8. timers -----------------------------------------------------------------
async def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


async def timer_checks():
    print("\n# timers")
    notifications.clear()
    spoken.clear()

    result = await dispatch("set_timer", {"seconds": 0.2, "label": "pasta"})
    check(result["ok"] and result["timer_id"] and result["label"] == "pasta" and "restarting" in result["note"],
          f"set_timer starts a timer and says it won't survive a restart ({result['message']})")
    listed = await dispatch("list_timers", {})
    check(listed["count"] == 1 and listed["timers"][0]["label"] == "pasta" and 0 < listed["timers"][0]["remaining_seconds"] <= 0.2
          and "restarting" in listed["note"], f"list_timers shows it ({listed['message']})")
    check(not notifications and not spoken, "nothing is announced before it is due")
    await wait_for(lambda: spoken)
    check(notifications == [{"title": "Timer: pasta", "message": "Your pasta timer is done."}], "firing shows a notification through show_notification")
    check(spoken == [{"websocket": None, "text": "Your pasta timer is done."}], "firing speaks through tts.speak (no UI connected: websocket None)")
    check((await dispatch("list_timers", {}))["count"] == 0, "a fired timer is gone from the list")
    fired = events("timer_fired")
    check(len(fired) == 1 and fired[0]["label"] == "pasta" and fired[0]["notified"] and fired[0]["late_by"] < 0.5, "timer_fired is logged")

    notifications.clear(); spoken.clear()
    await dispatch("set_timer", {"seconds": 0.1, "message": "Time to take the pizza out."})
    await dispatch("set_timer", {"minutes": 0.03})
    await wait_for(lambda: len(spoken) == 2)
    check([s["text"] for s in spoken] == ["Timer reminder: Time to take the pizza out.", "Your 2 second timer is done."],
          f"a custom message is what gets spoken (a reminder); an unlabelled timer is named by its length {[s['text'] for s in spoken]}")
    check(notifications[0] == {"title": "Timer done", "message": "Timer reminder: Time to take the pizza out."}, "and what the banner says")

    # hours + minutes + seconds add up
    result = await dispatch("set_timer", {"hours": 1, "minutes": 30, "seconds": 15, "label": "  long   one  "})
    check(result["duration"] == "1 hour 30 minutes" and result["label"] == "long one" and 5414 < result["remaining_seconds"] <= 5415, "hours, minutes and seconds add together")

    # cancel
    notifications.clear(); spoken.clear()
    short = await dispatch("set_timer", {"seconds": 0.25, "label": "tea"})
    result = await dispatch("cancel_timer", {})
    check(result["ok"] is False and "more than one" in result["error"] and len(result["timers"]) == 2,
          "cancel with nothing named and two running refuses and lists them")
    result = await dispatch("cancel_timer", {"timer_id": short["timer_id"]})
    check(result["ok"] and result["cancelled"][0]["label"] == "tea" and result["remaining_count"] == 1, "cancel by id")
    await asyncio.sleep(0.4)
    check(not spoken and not notifications, "a cancelled timer never fires")
    result = await dispatch("cancel_timer", {"label": "LONG"})
    check(result["ok"] and result["remaining_count"] == 0, "cancel by part of the label, case-insensitively")
    check((await dispatch("cancel_timer", {}))["ok"] is False, "cancel with nothing running is an error")
    check((await dispatch("cancel_timer", {"timer_id": "999"}))["ok"] is False, "cancel of an unknown id is an error")
    check((await dispatch("cancel_timer", {"label": "nope"}))["ok"] is False, "cancel of an unknown label is an error")
    only = await dispatch("set_timer", {"seconds": 30})
    result = await dispatch("cancel_timer", {})
    check(result["ok"] and result["cancelled"][0]["timer_id"] == only["timer_id"], "with exactly one running, 'cancel the timer' needs no id")
    result = await dispatch("cancel_timer", {"all": True})
    check(result["ok"] and result["cancelled"] == [], "cancel all with none running is a no-op, not an error")

    # limits
    for bad, label in [({}, "no duration"), ({"seconds": 0}, "zero"), ({"seconds": -5}, "negative"), ({"minutes": True}, "a boolean"),
                       ({"seconds": "10"}, "text"), ({"hours": 25}, "25 hours"), ({"seconds": float("nan")}, "NaN"),
                       ({"seconds": float("inf")}, "infinity")]:
        result = await dispatch("set_timer", bad)
        check(result["ok"] is False, f"set_timer rejects {label} ({result.get('error')})")
    check((await dispatch("set_timer", {"hours": 24}))["ok"], "exactly 24 hours is allowed")
    await dispatch("cancel_timer", {"all": True})
    for i in range(timers.MAX_TIMERS):
        await dispatch("set_timer", {"seconds": 60, "label": f"t{i}"})
    result = await dispatch("set_timer", {"seconds": 60})
    check(result["ok"] is False and "already 20 timers" in result["error"] and len(timers.active()) == 20, "the 21st timer is refused")
    result = await dispatch("cancel_timer", {"all": True})
    check(result["ok"] and len(result["cancelled"]) == 20 and timers.active() == [], "cancel all clears them")
    long_label = await dispatch("set_timer", {"seconds": 60, "label": "x" * 500, "message": "y" * 500})
    check(len(long_label["label"]) == 60 and len(next(iter(timers._timers.values()))["message"]) == 200, "label and message are capped")
    await dispatch("cancel_timer", {"all": True})

    # concurrent timers all fire, in order
    notifications.clear(); spoken.clear()
    for delay, label in [(0.30, "c"), (0.10, "a"), (0.20, "b"), (0.40, "d"), (0.15, "a2")]:
        await dispatch("set_timer", {"seconds": delay, "label": label})
    check([t["label"] for t in timers.active()] == ["a", "a2", "b", "c", "d"], "list is soonest first")
    await wait_for(lambda: len(spoken) == 5)
    check([n["title"] for n in notifications] == [f"Timer: {x}" for x in ["a", "a2", "b", "c", "d"]], "five concurrent timers all fire, in order")

    # a banner failure must not cost the spoken line
    for mode in ("fail", "raise"):
        notifications.clear(); spoken.clear()
        notify_mode["mode"] = mode
        await dispatch("set_timer", {"seconds": 0.05, "label": mode})
        await wait_for(lambda: spoken)
        check(len(spoken) == 1 and len(notifications) == 1, f"notification {mode}s: the timer is still spoken")
    notify_mode["mode"] = "ok"
    check(events("timer_fired")[-1]["notified"] is False, "and the log says the banner failed")
    notifications.clear(); spoken.clear()
    preferences.set_preference("show_notification", enabled=False)
    await dispatch("set_timer", {"seconds": 0.05})
    await wait_for(lambda: spoken)
    check(len(spoken) == 1 and notifications == [], "show_notification disabled in the Library: no banner from a timer either")
    preferences.clear("show_notification")

    # wall clock, not monotonic: a timer whose time passed while asleep fires on waking
    notifications.clear(); spoken.clear()
    offset = {"s": 0.0}
    timers._clock = lambda: time.time() + offset["s"]
    old_poll, timers.POLL_S = timers.POLL_S, 0.05
    await dispatch("set_timer", {"minutes": 10, "label": "nap"})
    await asyncio.sleep(0.15)
    check(not spoken, "a ten-minute timer has not fired after 150 ms")
    offset["s"] = 601.0  # the Mac "slept" for ten minutes
    fired_in = time.monotonic()
    await wait_for(lambda: spoken)
    check(len(spoken) == 1 and time.monotonic() - fired_in < 0.5, "it fires within one poll of the wall clock passing its time")
    timers._clock, timers.POLL_S = time.time, old_poll

    # which connection the line goes out on
    notifications.clear(); spoken.clear()
    primary = object()
    fake_server = types.ModuleType("agent.core.server")
    fake_server._primary = lambda: (primary, {}, {})
    sys.modules["agent.core.server"] = fake_server
    await dispatch("set_timer", {"seconds": 0.05})
    await wait_for(lambda: spoken)
    check(spoken[0]["websocket"] is primary, "with a UI connected the line is spoken on the server's primary connection")
    spoken.clear()
    fake_server._primary = lambda: (None, {}, {})
    await dispatch("set_timer", {"seconds": 0.05})
    await wait_for(lambda: spoken)
    check(spoken[0]["websocket"] is None, "with none connected it is still spoken")
    del sys.modules["agent.core.server"]

    # outside a running loop
    check(timers.active() == [], "no timers left running")


asyncio.run(timer_checks())
result = TOOLS["set_timer"]["fn"](seconds=5)
check(result["ok"] is False and "while the agent is running" in result["error"], "set_timer outside the event loop is a clean error")

# --- 9. duration parsing ---------------------------------------------------------
print("\n# quick_timer durations")
for text, expected in [("10 minutes", 600), ("10 min", 600), ("90 seconds", 90), ("1 hour 30 minutes", 5400), ("1h30m", 5400),
                       ("1 hour and 30 minutes", 5400), ("an hour and a half", 5400), ("half an hour", 1800), ("a minute", 60),
                       ("two and a half minutes", 150), ("2.5 hours", 9000), ("ten minutes", 600), ("forty-five seconds", 45),
                       ("2:30", 150), ("1:00:00", 3600), ("for 5 minutes", 300), ("  3   MINS ", 180), ("0.2 seconds", 0.2),
                       ("1 hour, 5 minutes and 10 seconds", 3910)]:
    got = quick_timer.parse_duration(text)
    check(got is not None and abs(got - expected) < 1e-9, f"{text!r} -> {expected} seconds")
for text in ["", "soon", "10", "10 months", "twenty five minutes", "ten", "5 minutes ago", "minutes", "0 minutes", None, 600, "3 mins or so"]:
    check(quick_timer.parse_duration(text) is None, f"{text!r} is not guessed at")


# --- 10. skills, end to end through dispatch() -----------------------------------
canned_tools = {
    "get_battery_status": {"ok": True, "has_battery": True, "percent": 80, "state": "charging"},
    "get_disk_space": {"ok": True, "total_gb": 500.0, "used_gb": 300.0, "free_gb": 200.0},
    "get_current_datetime": {"ok": True, "iso": "2026-10-12T08:05:00", "human": "Monday, October 12, 2026 at 08:05 AM"},
    "get_system_info": {"ok": True, "cpu_load_percent": 12, "memory": {"used_percent": 50, "pressure": "normal"},
                        "uptime_seconds": 3600, "uptime": "1 hour", "message": "fine"},
    "get_network_status": {"ok": True, "connected": True, "internet": True, "kind": "Wi-Fi", "message": "Connected over Wi-Fi."},
}
REAL_FNS = {name: TOOLS[name]["fn"] for name in canned_tools}
for name in canned_tools:
    TOOLS[name]["fn"] = (lambda n: lambda: dict(canned_tools[n]))(name)


async def skill_checks():
    pending = {}
    ui = FakeWebSocket(pending)

    async def skill(name, **args):
        return await dispatch(name, args, ui, pending)

    def reset(level=44, muted=False):
        fake_volume.level, fake_volume.muted = level, muted
        notifications.clear(); spoken.clear(); opened_apps.clear(); ui.sent.clear()
        timers.reset()

    print("\n# skill: morning_briefing")
    reset()
    result = await skill("morning_briefing")
    check(result["ok"] and step_tools(result) == ["get_current_datetime", "get_battery_status", "get_disk_space"],
          "no home, no place: the weather step is skipped, not failed")
    check(result["message"].startswith("Good morning. It's Monday, October 12") and "tell me your city" in result["message"],
          f"message: {result['message']}")
    await dispatch("set_home_location", {"place": "Austin, Texas"})
    result = await skill("morning_briefing")
    check(result["ok"] and step_tools(result)[-1] == "get_weather" and result["steps"][-1]["args"] == {"days": 1}
          and "In Austin, Texas it's" in result["message"] and "plugged in" in result["message"], "with a home location it includes the weather")
    result = await skill("morning_briefing", place="Paris")
    check(result["ok"] and result["steps"][-1]["args"] == {"place": "Paris", "days": 1} and "In Paris" in result["message"], "a named place wins over home")
    canned_tools["get_disk_space"]["free_gb"] = 4.2
    canned_tools["get_battery_status"].update(percent=11, state="discharging")
    canned_tools["get_current_datetime"]["iso"] = "2026-10-12T19:30:00"
    result = await skill("morning_briefing")
    check("Good evening." in result["message"] and "plug in soon" in result["message"] and "4.2 gigabytes free" in result["message"],
          "low battery and low disk are called out; greeting follows the clock")
    OpenMeteo.mode = "error500"
    result = await skill("morning_briefing")
    check(result["ok"] is False and result["failed_step"] == "get_weather" and len(result["steps"]) == 4
          and "Steps completed before it: get_current_datetime, get_battery_status, get_disk_space" in result["error"],
          f"weather down: stops there and says which step ({result['error'][:60]}...)")
    OpenMeteo.mode = "ok"
    check((await skill("morning_briefing", place=5))["ok"] is False, "a non-text place is rejected before any step")
    canned_tools["get_disk_space"]["free_gb"] = 200.0
    canned_tools["get_battery_status"].update(percent=80, state="charging")
    canned_tools["get_current_datetime"]["iso"] = "2026-10-12T08:05:00"

    print("\n# skill: system_health_check")
    result = await skill("system_health_check")
    check(result["ok"] and result["healthy"] and step_tools(result) == ["get_system_info", "get_disk_space", "get_battery_status", "get_network_status"]
          and result["message"].startswith("Your Mac looks healthy"), f"healthy: {result['message']}")
    canned_tools["get_battery_status"].update(percent=9, state="discharging")
    canned_tools["get_network_status"].update(internet=False)
    canned_tools["get_system_info"]["memory"] = {"used_percent": 95, "pressure": "critical"}
    result = await skill("system_health_check")
    check(result["ok"] and not result["healthy"] and len(result["issues"]) == 3 and result["message"].startswith("3 things to look at"),
          f"three problems are named: {result['message'][:90]}...")
    canned_tools["get_network_status"] = {"ok": False, "error": "could not work out the active network interface"}
    result = await skill("system_health_check")
    check(result["ok"] is False and result["failed_step"] == "get_network_status", "a failed check stops the skill and is named")
    canned_tools["get_network_status"] = {"ok": True, "connected": True, "internet": True, "kind": "Wi-Fi", "message": "ok"}
    canned_tools["get_battery_status"].update(percent=80, state="charging")
    canned_tools["get_system_info"]["memory"] = {"used_percent": 50, "pressure": "normal"}

    print("\n# skill: weather_report")
    result = await skill("weather_report")
    check(result["ok"] and result["days"] == 3 and result["steps"][0]["args"] == {"days": 3}
          and "Today: partly cloudy" in result["message"] and "Tomorrow: light rain" in result["message"] and "Wednesday: clear" in result["message"],
          f"three days by default, named: {result['message'][:80]}...")
    result = await skill("weather_report", place="Paris", days=7)
    check(result["ok"] and result["days"] == 7 and result["place"].startswith("Paris"), "a place and seven days")
    for bad in [{"days": 1}, {"days": 8}, {"days": "three"}, {"place": 3}]:
        result = await skill("weather_report", **bad)
        check(result["ok"] is False and result["steps"] == [], f"{bad} rejected before any step")
    weather.HOME_FILE.unlink()
    result = await skill("weather_report")
    check(result["ok"] is False and result["failed_step"] == "get_weather" and "Ask which city" in result["error"],
          "no home and no place: fails with the instruction to ask")

    print("\n# skill: quick_timer")
    reset()
    result = await skill("quick_timer", duration="0.2 seconds", label="tea")
    check(result["ok"] and step_tools(result) == ["set_timer", "show_notification"] and result["steps"][0]["args"] == {"seconds": 0.2, "label": "tea"},
          "parses the duration, sets the timer, shows a banner")
    check(notifications[0]["title"] == "Timer started" and "ending at" in notifications[0]["message"], "the banner says when it ends")
    await wait_for(lambda: spoken)
    check(spoken[0]["text"] == "Your tea timer is done." and len(notifications) == 2, "and the timer really fires")
    result = await skill("quick_timer", duration="a while")
    check(result["ok"] is False and result["steps"] == [] and timers.active() == [], "an unreadable duration sets nothing")
    check((await skill("quick_timer"))["ok"] is False, "a missing duration is an error, not a crash")
    result = await skill("quick_timer", duration="30 hours")
    check(result["ok"] is False and result["failed_step"] == "set_timer" and "24 hours" in result["error"], "set_timer's own limit surfaces as the failed step")
    notify_mode["mode"] = "fail"
    result = await skill("quick_timer", duration="10 minutes")
    check(result["ok"] is False and result["failed_step"] == "show_notification" and "Steps completed before it: set_timer" in result["error"]
          and len(timers.active()) == 1, "banner fails: reported, and it says the timer step already ran")
    notify_mode["mode"] = "ok"

    print("\n# skill: pomodoro")
    reset()
    result = await skill("pomodoro", focus_minutes=0.003, break_minutes=0.002, task="the report")
    check(result["ok"] and step_tools(result) == ["set_timer", "set_timer", "show_notification"]
          and [t["label"] for t in timers.active()] == ["pomodoro focus", "pomodoro break"], "sets a focus timer and a break timer after it")
    check(abs(result["steps"][1]["args"]["minutes"] - 0.005) < 1e-9, "the break timer runs for focus + break")
    await wait_for(lambda: len(spoken) == 2)
    check([s["text"] for s in spoken] == ["Timer reminder: Focus session done on the report. Take a 0.002 minute break.", "Timer reminder: Break's over. Ready for the next one."],
          "both announce themselves, in order")
    reset()
    result = await skill("pomodoro")
    check(result["ok"] and "25 minutes of focus" in result["message"] and "5 minute break" in result["message"]
          and 1499 < timers.active()[0]["remaining_seconds"] <= 1500 and 1799 < timers.active()[1]["remaining_seconds"] <= 1800,
          f"defaults are 25 and 5 ({result['message']})")
    reset()
    for i in range(timers.MAX_TIMERS - 1):
        timers.create(60, label=f"fill{i}")
    result = await skill("pomodoro")
    check(result["ok"] is False and result["failed_step"] == "set_timer" and step_tools(result) == ["set_timer", "set_timer", "cancel_timer"]
          and "cancelled again" in result["error"] and len(timers.active()) == timers.MAX_TIMERS - 1,
          "no room for the break timer: the focus timer is rolled back")
    for bad in [{"focus_minutes": 0}, {"focus_minutes": 500}, {"break_minutes": -1}, {"focus_minutes": "25"}, {"task": 7}]:
        check((await skill("pomodoro", **bad))["ok"] is False, f"{bad} rejected")

    print("\n# skill: focus_mode")
    reset(level=44, muted=False)
    result = await skill("focus_mode", minutes=0.002)
    check(result["ok"] and step_tools(result) == ["get_volume", "set_volume", "set_timer", "show_notification"]
          and fake_volume.muted and fake_volume.level == 44 and result["previous_muted"] is False, "mutes (level kept) and starts a focus timer")
    check("Notifications are not silenced" in result["message"], "says plainly that it is not Do Not Disturb")
    await wait_for(lambda: spoken)
    check("still muted" in spoken[0]["text"] and fake_volume.muted, "the timer's line says sound is still muted; nothing unmutes by itself")
    reset(level=44, muted=True)
    result = await skill("focus_mode")
    check(result["ok"] and step_tools(result) == ["get_volume", "set_timer", "show_notification"] and "already muted" in result["message"],
          "already muted: no set_volume step")
    reset()
    fake_volume.no_control = True
    result = await skill("focus_mode")
    check(result["ok"] is False and result["failed_step"] == "get_volume" and timers.active() == [], "no volume control: stops before setting a timer")
    fake_volume.no_control = False

    print("\n# skill: meeting_prep")
    reset(level=10, muted=True)
    result = await skill("meeting_prep", app="Zoom", minutes=0.002)
    check(result["ok"] and step_tools(result) == ["get_network_status", "get_volume", "set_volume", "get_battery_status", "open_app", "set_timer"],
          "a call: network, sound, battery, app, timer")
    check(fake_volume.level == 40 and not fake_volume.muted and opened_apps == ["Zoom"] and "Sound is on at 40 percent" in result["message"],
          "a muted, quiet Mac is unmuted and raised to 40 for a call")
    await wait_for(lambda: spoken)
    check(spoken[0]["text"] == "Timer reminder: Your meeting time is up.", "the meeting timer fires")
    reset(level=60, muted=False)
    result = await skill("meeting_prep")
    check(result["ok"] and step_tools(result) == ["get_network_status", "get_volume", "get_battery_status"] and fake_volume.level == 60
          and opened_apps == [] and timers.active() == [], "audible already: volume untouched, nothing opened, no timer")
    reset(level=60, muted=True)
    await skill("meeting_prep")
    check(fake_volume.level == 60 and not fake_volume.muted, "muted but loud enough: unmuted, level left alone")
    reset(level=60, muted=False)
    result = await skill("meeting_prep", kind="in_person")
    check(result["ok"] and step_tools(result) == ["get_volume", "set_volume", "get_battery_status"] and fake_volume.muted
          and "Sound is muted" in result["message"], "in person: muted, no network check")
    canned_tools["get_network_status"].update(internet=False)
    result = await skill("meeting_prep")
    check(result["ok"] and "Heads up" in result["message"], "a call with no internet gets a warning, not a failure")
    canned_tools["get_network_status"].update(internet=True)
    for bad in [{"kind": "webinar"}, {"minutes": 0}, {"minutes": 9999}, {"app": 4}]:
        check((await skill("meeting_prep", **bad))["ok"] is False, f"{bad} rejected")

    print("\n# skill: end_of_day")
    reset(level=44, muted=False)
    timers.create(600, label="laundry")
    canned_tools["get_system_info"].update(uptime_seconds=9 * 86400, uptime="9 days")
    canned_tools["get_battery_status"].update(percent=35, state="discharging")
    scripts_before = len(fake_volume.scripts)
    result = await skill("end_of_day")
    check(result["ok"] and step_tools(result) == ["get_current_datetime", "list_timers", "get_battery_status", "get_disk_space", "get_system_info"],
          "five read-only steps (no home location: no weather step)")
    check("laundry" in result["message"] and "plug in overnight" in result["message"] and "9 days" in result["message"],
          f"message: {result['message']}")
    check(len(timers.active()) == 1 and len(fake_volume.scripts) == scripts_before and notifications == [],
          "it changes nothing: the timer still runs, volume untouched, no banner")
    await dispatch("set_home_location", {"place": "Paris"})
    result = await skill("end_of_day")
    check(result["ok"] and step_tools(result)[-1] == "get_weather" and "Tomorrow in Paris, Ile-de-France: light rain" in result["message"],
          "with a home location it adds tomorrow's forecast")
    weather.HOME_FILE.unlink()
    canned_tools["get_system_info"].update(uptime_seconds=3600, uptime="1 hour")
    canned_tools["get_battery_status"].update(percent=80, state="charging")

    print("\n# skill: wind_down")
    reset(level=44, muted=False)
    result = await skill("wind_down", minutes=0.002)
    check(result["ok"] and step_tools(result) == ["get_volume", "set_volume", "set_timer", "show_notification"]
          and fake_volume.level == 20 and result["previous_level"] == 44, "lowers the volume to 20 and sets a nudge")
    await wait_for(lambda: spoken)
    check(spoken[0]["text"] == "Timer reminder: Time to call it a night.", "the nudge fires")
    reset(level=10)
    result = await skill("wind_down", volume=30)
    check(result["ok"] and "set_volume" not in step_tools(result) and fake_volume.level == 10 and "already low" in result["message"],
          "already quieter than the target: never raises the volume")
    for bad in [{"volume": 101}, {"volume": -1}, {"minutes": 0}, {"minutes": "soon"}]:
        check((await skill("wind_down", **bad))["ok"] is False, f"{bad} rejected")

    print("\n# skill: battery_check")
    reset()
    result = await skill("battery_check")
    check(result["ok"] and step_tools(result) == ["get_battery_status"] and not result["needs_power"] and notifications == [],
          f"charging: one step, no banner ({result['message']})")
    canned_tools["get_battery_status"].update(percent=15, state="discharging")
    result = await skill("battery_check")
    check(result["ok"] and result["needs_power"] and step_tools(result) == ["get_battery_status", "show_notification"]
          and notifications == [{"title": "Battery low", "message": "15 percent and not charging. Plug in soon."}], "low and discharging: warning banner")
    notifications.clear()
    result = await skill("battery_check", threshold=10)
    check(result["ok"] and not result["needs_power"] and notifications == [], "the threshold is respected")
    canned_tools["get_battery_status"] = {"ok": True, "has_battery": False}
    result = await skill("battery_check")
    check(result["ok"] and "mains power" in result["message"], "a Mac with no battery is not an error")
    canned_tools["get_battery_status"] = {"ok": True, "has_battery": True, "percent": 80, "state": "charging"}
    check((await skill("battery_check", threshold=0))["ok"] is False, "threshold 0 rejected")

    print("\n# skills obey the same gates as direct calls")
    reset(level=44, muted=False)
    preferences.set_preference("set_volume", requires_confirmation=True)
    result = await skill("focus_mode", minutes=5)
    asked = [m for m in ui.sent if m.get("type") == "confirmation_required"]
    check(result["ok"] and len(asked) == 1 and asked[0]["tool"] == "set_volume" and asked[0]["args"] == {"muted": True} and fake_volume.muted,
          "a step the user marked ask-first pauses for confirmation inside the skill, then runs on approval")
    reset(level=44, muted=False)
    declining_pending = {}
    declining = FakeWebSocket(declining_pending, approve=False)
    result = await dispatch("focus_mode", {"minutes": 5}, declining, declining_pending)
    check(result["ok"] is False and result["failed_step"] == "set_volume" and "declined" in result["error"]
          and not fake_volume.muted and timers.active() == [], "declined: the skill stops there, nothing muted, no timer")
    result = await dispatch("focus_mode", {"minutes": 5})
    check(result["ok"] is False and result["failed_step"] == "set_volume" and not fake_volume.muted, "no UI to ask: fails closed")
    preferences.clear("set_volume")
    preferences.set_preference("set_timer", enabled=False)
    result = await skill("wind_down")
    check(result["ok"] is False and result["failed_step"] == "set_timer" and "disabled" in result["error"]
          and "Steps completed before it: get_volume, set_volume" in result["error"],
          "a step disabled in the Library fails the skill, which says what it had already changed")
    preferences.clear("set_timer")
    preferences.set_preference("pomodoro", enabled=False)
    check((await skill("pomodoro"))["ok"] is False and timers.active() == [], "a disabled skill does not run")
    preferences.clear("pomodoro")
    timers.reset()


asyncio.run(skill_checks())
for name, fn in REAL_FNS.items():
    TOOLS[name]["fn"] = fn


# --- 11. the real server: a timer set over a websocket speaks on that connection --
async def server_checks():
    print("\n# timers through the real server (ephemeral port, no voice session)")
    import secrets

    import websockets

    from agent.core import server, ws_auth

    # Through server.serve(), so this connection is authenticated like any
    # other; and manual dispatch ({"tool": ...}) is off unless enabled.
    auth = ws_auth.Auth(secrets.token_hex(32), ws_auth.SOURCE_ENV)
    server.MANUAL_DISPATCH = True

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    check(port != 8765, f"using port {port}, not 8765")
    notifications.clear(); spoken.clear()

    async with server.serve(auth, port):
        async with websockets.connect(f"ws://127.0.0.1:{port}", additional_headers=ws_auth.bearer(auth.token)) as ui:
            await ui.send(json.dumps({"type": "inventory", "request_id": "1"}))
            inventory = json.loads(await asyncio.wait_for(ui.recv(), 10))
            names = {t["name"] for t in inventory["tools"]}
            skills = {s["name"] for s in inventory["skills"]}
            check(set(SYSTEM_TOOLS) <= names and set(SYSTEM_SKILLS) <= skills, "inventory lists all twelve tools and ten skills")
            await ui.send(json.dumps({"type": "command", "tool": "set_timer", "args": {"seconds": 0.2, "label": "tea"}}))
            reply = json.loads(await asyncio.wait_for(ui.recv(), 10))
            check(reply["type"] == "tool_result" and reply["result"]["ok"], "set_timer over the websocket")
            await wait_for(lambda: spoken)
            check(len(spoken) == 1 and isinstance(spoken[0]["websocket"], server._MirroringSocket)
                  and spoken[0]["websocket"] is server._primary()[0],
                  "when it fires, the line is spoken on that UI's connection (the primary, via its mirroring socket)")
            check(notifications == [{"title": "Timer: tea", "message": "Your tea timer is done."}], "and the banner goes through dispatch")
            await ui.send(json.dumps({"type": "command", "tool": "set_timer", "args": {"seconds": 0.3, "label": "orphan"}}))
            await asyncio.wait_for(ui.recv(), 10)
        spoken.clear()
        await wait_for(lambda: spoken)
        check(len(spoken) == 1 and spoken[0]["websocket"] is None,
              "the UI disconnected before it fired: the timer outlives the connection and is still spoken")
    check(timers.active() == [], "no timers left running")


asyncio.run(server_checks())

# --- 12. the machine is as we found it --------------------------------------------
print("\n# nothing real was touched")
volume_tool._osascript = REAL_OSASCRIPT
if "error" not in real_before:
    check(volume_tool.read_volume() == real_before, f"real volume is still {real_before['level']} (muted {real_before['muted']})")
check(all(FakeVolume.SET.fullmatch(s) or s == "get volume settings" for s in fake_volume.scripts),
      "every volume script the tools produced was one of the two exact forms")
check(TOOLS["show_notification"]["fn"] is fake_notify and REAL_NOTIFY is not fake_notify, "the real show_notification was never reachable")
check(media_posts == [] or set(media_posts) <= {16, 17, 18}, "media keys only ever went to the recorder")
now_state = REAL_HOME_FILE.stat().st_mtime_ns if REAL_HOME_FILE.exists() else None
check(now_state == REAL_HOME_STATE, "the real home_location.json was not created or changed")
check(not (AGENT_DIR / "preferences.json").exists() or preferences.PREFS_FILE != AGENT_DIR / "preferences.json", "preferences went to the temp dir")

http.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")
