import os
import platform
import re
import time

from agent.core import bounded
from agent.core.timers import describe_duration

# Read-only, fixed argv, short timeouts. Called through the module attribute
# so the check script can feed canned sysctl/vm_stat output.
_run = bounded.run

GB = 1024**3
PRESSURE = {"1": "normal", "2": "warning", "4": "critical"}


def _sysctl(name: str) -> str:
    code, out, _ = _run(["sysctl", "-n", name], 3.0)
    return out.strip() if code == 0 else ""


def _memory() -> dict:
    """Used/available memory the way Activity Monitor counts it: app memory
    (anonymous minus purgeable) + wired + compressed is "used"; the rest is
    cache the system gives back on demand, so it counts as available."""
    total = _sysctl("hw.memsize")
    code, out, _ = _run(["vm_stat"], 3.0)
    if not total.isdigit() or code != 0:
        return {}
    page = re.search(r"page size of (\d+) bytes", out)
    pages = {m.group(1).strip().lower(): int(m.group(2)) for m in re.finditer(r"^\"?([^:\"]+)\"?:\s+(\d+)\.", out, re.M)}
    if not page or "pages wired down" not in pages:
        return {}
    size = int(page.group(1))
    used_pages = (
        max(0, pages.get("anonymous pages", pages.get("pages active", 0)) - pages.get("pages purgeable", 0))
        + pages["pages wired down"]
        + pages.get("pages occupied by compressor", 0)
    )
    total_bytes = int(total)
    used = min(total_bytes, used_pages * size)
    memory = {
        "total_gb": round(total_bytes / GB, 1),
        "used_gb": round(used / GB, 1),
        "available_gb": round((total_bytes - used) / GB, 1),
        "used_percent": round(100 * used / total_bytes),
    }
    pressure = PRESSURE.get(_sysctl("kern.memorystatus_vm_pressure_level"))
    if pressure:
        memory["pressure"] = pressure
    return memory


def _uptime_seconds():
    match = re.search(r"sec = (\d+)", _sysctl("kern.boottime"))
    if not match:
        return None
    return max(0, int(time.time()) - int(match.group(1)))


def get_system_info() -> dict:
    cores = os.cpu_count() or 1
    try:
        one, five, fifteen = os.getloadavg()
    except OSError:
        one = five = fifteen = None

    info = {
        "ok": True,
        "macos_version": platform.mac_ver()[0],
        "model": _sysctl("hw.model"),
        "chip": _sysctl("machdep.cpu.brand_string"),
        "cpu_cores": cores,
    }
    said = []
    if one is not None:
        # Load average counts runnable processes; divided by cores it reads
        # as "how busy", which is the only form worth saying aloud.
        info["load_average"] = {"1m": round(one, 2), "5m": round(five, 2), "15m": round(fifteen, 2)}
        info["cpu_load_percent"] = round(100 * one / cores)
        said.append(f"CPU load is about {info['cpu_load_percent']} percent")
    memory = _memory()
    if memory:
        info["memory"] = memory
        said.append(f"memory is {memory['used_percent']} percent used, {memory['used_gb']} of {memory['total_gb']} gigabytes")
    uptime = _uptime_seconds()
    if uptime is not None:
        info["uptime_seconds"] = uptime
        info["uptime"] = describe_duration(uptime) if uptime < 86400 else _days(uptime)
        said.append(f"it has been up for {info['uptime']}")
    text = ", ".join(said) + "."
    info["message"] = text[0].upper() + text[1:] if said else "No system figures were available."
    return info


def _days(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    hours = rest // 3600
    text = f"{days} day{'' if days == 1 else 's'}"
    return text + (f" {hours} hour{'' if hours == 1 else 's'}" if hours else "")
