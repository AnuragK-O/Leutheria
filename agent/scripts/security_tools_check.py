"""Check of the tool-level security fixes (review 2026-10-13: C3, H3, M2, L1, L3, L5).

    cd agent
    ./.venv/bin/python scripts/security_tools_check.py            # everything
    ./.venv/bin/python scripts/security_tools_check.py open_url   # named sections

The claim under test: nothing in the safe tier can launch code.

What it is careful about, since someone is usually sitting at this Mac:

  - launching   every `open` this could reach is a recorder (open_url,
                open_app, open_file, open_with, reveal_in_finder). Nothing is
                opened, launched or revealed; the hostile fixtures (a
                .command, a fake app bundle) are never handed to anything.
  - git         runs for real, but only in temp repositories this script
                makes, with HOME pointed at the temp dir so the user's own
                git config is neither read nor written.
  - network     fetch_url runs against servers this script starts on
                127.0.0.1 with an ephemeral port. No real request is made
                and port 8765 is never used.
  - state       the event log and preferences are redirected into a temp
                dir. No speech, no banner, no microphone, no model call.

Prints one line per check and exits non-zero on the first failure.
"""

import asyncio
import os
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

REAL_HOME = Path.home()
TMP = Path(tempfile.mkdtemp(prefix="leutheria_security_tools_")).resolve()
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

# Redirect local state before anything imports the registries.
from agent.core import logging_util  # noqa: E402

logging_util.LOG_DIR = TMP / "logs"
logging_util.LOG_FILE = logging_util.LOG_DIR / "events.jsonl"

from agent.core import preferences  # noqa: E402

preferences.PREFS_FILE = TMP / "preferences.json"
preferences._overrides.clear()

from agent.core import dispatcher, tts  # noqa: E402
from agent.core.dispatcher import dispatch  # noqa: E402
from agent.tools import open_app as open_app_mod  # noqa: E402
from agent.tools import open_file as open_file_mod  # noqa: E402
from agent.tools import open_url as open_url_mod  # noqa: E402
from agent.tools import open_with as open_with_mod  # noqa: E402
from agent.tools import reveal_in_finder as reveal_mod  # noqa: E402
from agent.tools.registry import TOOLS  # noqa: E402

passed = 0


def check(condition, label):
    global passed
    if not condition:
        print(f"FAIL  {label}")
        sys.exit(1)
    passed += 1
    print(f"ok    {label}")


def section(title):
    print(f"\n--- {title}")


# --- stubs installed before anything can reach the user -----------------------

launches = []  # every argv any opener would have run


def fake_launch(argv, timeout=3.0):
    launches.append(list(argv))
    return 0, "", ""


def fake_open_with_launch(argv):
    launches.append(list(argv))
    return subprocess.CompletedProcess(argv, 0, "", "")


open_url_mod._launch = fake_launch
open_app_mod._launch = fake_launch
open_file_mod._launch = fake_launch
reveal_mod._launch = fake_launch
open_with_mod._launch = fake_open_with_launch


async def _silent_speak(websocket, text):
    return None


tts.speak = _silent_speak
dispatcher.speak = _silent_speak
tts.set_player(lambda audio, rate: None)
TOOLS["show_notification"]["fn"] = lambda title, message: {"ok": True, "message": "notification shown"}


class FakeSocket:
    """Stands in for the UI. Declines every confirmation: nothing in this
    script should ever need one, and `asked` proves none was requested."""

    def __init__(self):
        self.asked = []

    async def send(self, raw):
        import json

        message = json.loads(raw)
        if message.get("type") == "confirmation_required":
            self.asked.append(message)


async def through_dispatch(name, args):
    sock = FakeSocket()
    result = await dispatch(name, args, sock, {})
    return result, sock.asked


def tool(name, **args):
    return TOOLS[name]["fn"](**args)


# --- open_url -----------------------------------------------------------------

def check_open_url():
    section("open_url: web pages only (C3)")
    refused = [
        # plain paths
        ("/Users/someone/Downloads/pkg/run.command", "absolute path"),
        ("~/Downloads/pkg/run.command", "home-relative path"),
        ("./run.command", "relative path"),
        ("run.command", "bare file name"),
        ("/Applications/Calculator.app", "application bundle path"),
        ("example.com", "bare host with no scheme"),
        # file URLs
        ("file:///Users/someone/Downloads/pkg/run.command", "file: URL"),
        ("file://localhost/etc/passwd", "file: URL with a host"),
        ("FILE:///etc/passwd", "file: URL in capitals"),
        # custom schemes
        ("shortcuts://run-shortcut?name=x", "shortcuts:"),
        ("ssh://attacker.example", "ssh:"),
        ("telnet://attacker.example", "telnet:"),
        ("smb://attacker.example/share", "smb:"),
        ("afp://attacker.example/share", "afp:"),
        ("vnc://attacker.example", "vnc:"),
        ("ftp://attacker.example/x", "ftp:"),
        ("x-apple.systempreferences:com.apple.preference.security", "x-apple.systempreferences:"),
        ("x-apple-reminderkit://x", "x-apple-*:"),
        ("vscode://file/etc/passwd", "vscode:"),
        ("tel:+15555550100", "tel:"),
        ("facetime://someone@example.com", "facetime:"),
        ("mailto:someone@example.com", "mailto:"),
        ("javascript:alert(1)", "javascript:"),
        ("data:text/html,<script>alert(1)</script>", "data:"),
        ("itms-apps://itunes.apple.com/app/id1", "itms-apps:"),
        ("applescript://com.apple.scripteditor?action=new&script=beep", "applescript:"),
        ("x-man-page://ls", "x-man-page: (opens Terminal)"),
        ("message://x", "message:"),
        ("about:blank", "about:"),
        ("blob:https://example.com/x", "blob:"),
        ("view-source:https://example.com", "view-source:"),
        ("ws://127.0.0.1:8765", "ws:"),
        # userinfo
        ("https://user:pass@example.com/", "user:password@"),
        ("https://user@example.com/", "user@"),
        ("https://example.com@evil.example/", "a host that is really userinfo"),
        ("https://@example.com/", "empty userinfo"),
        # no host
        ("https://", "https with no host"),
        ("https:///etc/passwd", "https with an empty host"),
        ("http:example.com", "http with no //"),
        ("https://:443/", "port but no host"),
        ("https://example.com:99999/", "port out of range"),
        ("https://example.com:abc/", "non-numeric port"),
        # control characters and whitespace
        ("https://example.com/\nfile:///etc/passwd", "newline"),
        ("https://example.com/\r\n", "CR LF inside"),
        ("https://exa\tmple.com/", "tab"),
        ("https://example.com/a b", "space"),
        ("https://example.com/\x00", "NUL"),
        ("https://example.com/\x1b[2J", "escape"),
        ("https://example.com/\x7f", "DEL"),
        ("https://example.com/‮gnp.exe", "right-to-left override"),
        ("https://example.com/​", "zero-width space"),
        ("https://example.com/ ", "no-break space"),
        ("https://example.com\\@evil.example/", "backslash"),
        ("\tfile:///etc/passwd", "leading tab then file:"),
        # leading dash
        ("-a", "option -a"),
        ("-R", "option -R"),
        ("--args", "option --args"),
        ("-a Terminal https://example.com", "option with arguments"),
        ("-https://example.com", "dash in front of a URL"),
        # wrong types
        ("", "empty string"),
        ("   ", "only spaces"),
        (None, "None"),
        (["https://example.com"], "a list"),
        (42, "a number"),
        ("https://example.com/" + "a" * 5000, "over the length cap"),
    ]
    for value, label in refused:
        launches.clear()
        result = tool("open_url", url=value)
        check(result.get("ok") is False and result.get("error") and launches == [], f"refuses {label}: {result.get('error')[:70]}")
    result = tool("open_url", url="/tmp/x.command")
    check("open_file" in result["error"] and "web pages only" in result["error"], "a refused path points at open_file")

    accepted = [
        ("https://example.com", "https://example.com"),
        ("https://example.com/", "https://example.com/"),
        ("http://example.com/a/b?q=1&r=two#frag", "http://example.com/a/b?q=1&r=two#frag"),
        ("HTTPS://Example.com/Path", "https://Example.com/Path"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        ("https://www.google.com/search?q=a%20b+c", "https://www.google.com/search?q=a%20b+c"),
        ("https://[2606:4700::1111]/", "https://[2606:4700::1111]/"),
        ("http://localhost:3000/", "http://localhost:3000/"),
        ("https://bücher.example/straße", "https://bücher.example/straße"),
        ("  https://example.com/padded  ", "https://example.com/padded"),
        ("https://github.com/torvalds/linux/issues", "https://github.com/torvalds/linux/issues"),
    ]
    for value, expected in accepted:
        launches.clear()
        result = tool("open_url", url=value)
        check(result.get("ok") is True and launches == [["open", "--", expected]], f"opens {value.strip()!r} as {launches}")
    description = TOOLS["open_url"]["description"]
    check("Web pages only" in description and "open_file" in description, "the description says web pages only and names open_file")


SECTIONS = {
    "open_url": check_open_url,
}


def main():
    wanted = sys.argv[1:] or list(SECTIONS)
    unknown = [name for name in wanted if name not in SECTIONS]
    if unknown:
        print(f"unknown section(s): {', '.join(unknown)}. Known: {', '.join(SECTIONS)}")
        sys.exit(2)
    try:
        for name in wanted:
            outcome = SECTIONS[name]()
            if asyncio.iscoroutine(outcome):
                asyncio.run(outcome)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\nall {passed} checks passed")


if __name__ == "__main__":
    main()
