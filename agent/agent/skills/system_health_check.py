from agent.skills.system_steps import Steps, StepFailed, battery_phrase

LOW_DISK_GB = 15
BUSY_CPU_PERCENT = 85
HIGH_MEMORY_PERCENT = 90
LONG_UPTIME_DAYS = 14


async def run(args: dict, websocket, pending: dict) -> dict:
    """Four read-only checks -- CPU and memory, disk, battery, network --
    boiled down to "it's fine" or the specific things that aren't."""
    steps = Steps(websocket, pending)
    try:
        system = await steps.run("get_system_info")
        disk = await steps.run("get_disk_space")
        battery = await steps.run("get_battery_status")
        network = await steps.run("get_network_status")
    except StepFailed as failure:
        return steps.failed(failure)

    issues = []
    memory = system.get("memory") or {}
    if system.get("cpu_load_percent", 0) >= BUSY_CPU_PERCENT:
        issues.append(f"the processor is busy, at about {system['cpu_load_percent']} percent load")
    if memory.get("pressure") in ("warning", "critical") or memory.get("used_percent", 0) >= HIGH_MEMORY_PERCENT:
        issues.append(f"memory is tight, at {memory.get('used_percent')} percent used")
    if disk.get("free_gb") is not None and disk["free_gb"] < LOW_DISK_GB:
        issues.append(f"the disk is nearly full, with {disk['free_gb']} gigabytes free")
    battery_text, needs_power = battery_phrase(battery)
    if needs_power:
        issues.append(f"the battery is at {battery.get('percent')} percent and not charging")
    if not network.get("connected"):
        issues.append("there is no network connection")
    elif not network.get("internet"):
        issues.append("the network is connected but the internet is not reachable")
    if system.get("uptime_seconds", 0) > LONG_UPTIME_DAYS * 86400:
        issues.append(f"it has been running for {system.get('uptime')} without a restart")

    facts = []
    if "cpu_load_percent" in system:
        facts.append(f"CPU load {system['cpu_load_percent']} percent")
    if memory:
        facts.append(f"memory {memory['used_percent']} percent used")
    if disk.get("free_gb") is not None:
        facts.append(f"{disk['free_gb']} gigabytes of disk free")
    if battery.get("has_battery"):
        facts.append(f"battery {battery.get('percent')} percent")
    if network.get("connected"):
        facts.append(f"online over {network.get('kind')}" if network.get("internet") else "no internet")

    if not issues:
        message = "Your Mac looks healthy: " + ", ".join(facts) + "."
    else:
        count = "One thing" if len(issues) == 1 else f"{len(issues)} things"
        message = f"{count} to look at: " + "; ".join(issues) + ". Otherwise: " + ", ".join(facts) + "."
    return steps.done(message, healthy=not issues, issues=issues)
