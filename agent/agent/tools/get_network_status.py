import re
import socket

from agent.core import bounded

_run = bounded.run

# What "is the internet reachable" means here: a TCP connection to the host
# macOS itself uses for its captive-portal check. One SYN, no request, no
# payload, and nothing about the machine is looked up or sent anywhere --
# in particular there is no public-IP or location lookup. Overridable so the
# check script can point it at a local listener or a closed port.
PROBE = ("captive.apple.com", 80)
PROBE_TIMEOUT = 2.0


def _reachable() -> bool:
    def connect():
        with socket.create_connection(PROBE, timeout=PROBE_TIMEOUT):
            return True

    try:
        # The socket timeout doesn't cover a DNS lookup that hangs, hence the
        # outer deadline.
        return bool(bounded.call_with_deadline(connect, PROBE_TIMEOUT + 1.0))
    except (OSError, bounded.DeadlineExceeded):
        return False


def _port_name(interface: str) -> str:
    """'en0' -> 'Wi-Fi', from the hardware port list. A VPN's utun device
    isn't a hardware port, so it's named by what it is."""
    code, out, _ = _run(["networksetup", "-listallhardwareports"], 3.0)
    if code == 0:
        match = re.search(r"Hardware Port: (.+)\nDevice: " + re.escape(interface) + r"\n", out)
        if match:
            return match.group(1).strip()
    if interface.startswith(("utun", "ipsec", "ppp")):
        return "VPN"
    return interface


def get_network_status() -> dict:
    code, out, _ = _run(["route", "-n", "get", "default"], 3.0)
    interface = re.search(r"interface:\s*(\S+)", out) if code == 0 else None
    if not interface:
        return {
            "ok": True,
            "connected": False,
            "internet": False,
            "message": "This Mac is not connected to any network.",
        }
    interface = interface.group(1)
    if not re.fullmatch(r"[A-Za-z0-9]{1,15}", interface):
        return {"ok": False, "error": "could not work out the active network interface"}
    gateway = re.search(r"gateway:\s*(\S+)", out)

    kind = _port_name(interface)
    code, address, _ = _run(["ipconfig", "getifaddr", interface], 3.0)
    internet = _reachable()

    result = {
        "ok": True,
        "connected": True,
        "internet": internet,
        "interface": interface,
        "kind": kind,
        "message": f"Connected over {kind}" + (", and the internet is reachable." if internet else ", but the internet is not reachable."),
    }
    if code == 0 and address.strip():
        result["ip_address"] = address.strip()
    if gateway:
        result["router"] = gateway.group(1)
    return result
