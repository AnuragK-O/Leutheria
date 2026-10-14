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


def tool(tool_name, **args):
    return TOOLS[tool_name]["fn"](**args)


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


# --- open_app -----------------------------------------------------------------

def make_bundle(path: Path, executable: bool = True, display_name: str = None) -> Path:
    """A fake .app. Its 'program' is a text file that is never run."""
    import plistlib

    macos = path / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    binary = macos / path.stem
    binary.write_text("#!/bin/sh\necho this fixture is never run\n")
    binary.chmod(0o755 if executable else 0o644)
    info = {"CFBundleExecutable": path.stem, "CFBundleName": path.stem, "CFBundlePackageType": "APPL"}
    if display_name:
        info["CFBundleDisplayName"] = display_name
    with open(path / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    return path


def check_open_app():
    section("open_app: a name, resolved to an installed bundle (C3)")
    downloads = TMP / "app_case" / "Downloads"
    evil = make_bundle(downloads / "pkg" / "Evil.app")
    refused = [
        (str(evil), "a bundle path in Downloads"),
        ("/Applications/Safari.app", "a bundle path, even an installed one"),
        ("/System/Applications/Utilities/Terminal.app", "another bundle path"),
        ("~/Downloads/pkg/Evil.app", "a home-relative bundle path"),
        ("../../Downloads/pkg/Evil", "a .. path"),
        ("Utilities/../../Users/Shared/Evil", "a path through a real folder"),
        ("..", "just .."),
        ("Safari/../Evil", "a slash in the name"),
        ("Macintosh HD:Applications:Safari", "an HFS-style path"),
        ("-n", "option -n"),
        ("-W", "option -W"),
        ("--args", "--args"),
        ("-a Terminal", "option with a value"),
        ("-b com.apple.Terminal", "a bundle id option"),
        ("Safari\x00.app", "a NUL in the name"),
        ("Saf\nari", "a newline in the name"),
        ("Safari\x1b", "an escape in the name"),
        ("", "an empty name"),
        ("   ", "only spaces"),
        (None, "None"),
        (["Safari"], "a list"),
        ("A" * 300, "a very long name"),
        ("Evil", "a name that is only in Downloads"),
        ("No Such Application Anywhere", "a name that does not resolve"),
        ("run.command", "a script's name"),
    ]
    for value, label in refused:
        launches.clear()
        result = tool("open_app", name=value)
        check(result.get("ok") is False and result.get("error") and launches == [], f"refuses {label}: {result.get('error')[:60]}")
    check("never a path" in tool("open_app", name=str(evil))["error"], "a refused path says a name is needed")

    for name, expected in [
        ("Safari", "/Applications/Safari.app"),
        ("safari", "/Applications/Safari.app"),
        ("Safari.app", "/Applications/Safari.app"),
        ("TextEdit", "/System/Applications/TextEdit.app"),
        ("textedit.APP", "/System/Applications/TextEdit.app"),
        ("Text Edit", "/System/Applications/TextEdit.app"),
        ("Calculator", "/System/Applications/Calculator.app"),
        ("Terminal", "/System/Applications/Utilities/Terminal.app"),
        ("Finder", "/System/Library/CoreServices/Finder.app"),
    ]:
        launches.clear()
        result = tool("open_app", name=name)
        check(result.get("ok") is True and launches == [["open", "-a", expected]], f"{name!r} launches exactly {launches}")
    import inspect

    check(list(inspect.signature(open_app_mod.open_app).parameters) == ["name"]
          and list(TOOLS["open_app"]["input_schema"]["properties"]) == ["name"],
          "open_app takes one argument, so it cannot be handed a document")

    # The lookup itself, against roots this script controls.
    system_root = TMP / "app_case" / "SystemApps"
    user_root = TMP / "app_case" / "UserApps"
    make_bundle(system_root / "Notes.app")
    make_bundle(system_root / "Vendor" / "Suite Tool.app")
    make_bundle(system_root / "VoiceMemos.app", display_name="Voice Memos")
    make_bundle(system_root / "Host.app" / "Contents" / "Helpers" / "Hidden.app")
    make_bundle(system_root / "Vendor" / "Deeper" / "TooDeep.app")
    make_bundle(user_root / "Notes.app")
    make_bundle(user_root / "Mine.app")
    user_root.joinpath("Planted.app").symlink_to(evil)
    user_root.joinpath("LinkedFolder").symlink_to(downloads / "pkg")
    system_root.joinpath("Alias.app").symlink_to(system_root / "Notes.app")
    real_roots = open_app_mod.app_roots
    open_app_mod.app_roots = lambda: [system_root, user_root]
    try:
        resolve = open_app_mod.resolve_app
        check(resolve("notes")[0] == str(system_root / "Notes.app"), "the system folder wins over ~/Applications for the same name")
        check(resolve("Mine")[0] == str(user_root / "Mine.app"), "an app only in ~/Applications resolves there")
        check(resolve("Suite Tool")[0] == str(system_root / "Vendor" / "Suite Tool.app"), "an app in a vendor folder resolves")
        check(resolve("Voice Memos")[0] == str(system_root / "VoiceMemos.app"), "a spaced name matches VoiceMemos.app")
        check(resolve("Alias")[0] == str(system_root / "Alias.app"), "a link to a bundle inside the roots is fine (Safari is one)")
        check(resolve("Planted")[0] is None, "a link in ~/Applications to a bundle in Downloads is refused")
        check(resolve("Evil")[0] is None, "and so is that bundle through a linked folder")
        check(resolve("Hidden")[0] is None, "an app inside another app's bundle is not an installed application")
        check(resolve("TooDeep")[0] is None, "nor is one two folders down")
    finally:
        open_app_mod.app_roots = real_roots
    description = TOOLS["open_app"]["description"]
    check("never a path" in description and "open_file" in description, "the description says name only")


# --- extract_archive + chain D ---------------------------------------------------

QUARANTINE_VALUE = b"0083;6700f000;Safari;00000000-0000-0000-0000-000000000000"


def check_extract_archive():
    from agent.tools import extract_archive as ex

    section("extract_archive: no execute bits, quarantine carried over (C3, L3)")
    work = TMP / "chain_d" / "Downloads"
    work.mkdir(parents=True)
    archive = work / "pkg.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name, mode, data in [
            ("pkg/run.command", 0o100755, b"#!/bin/sh\necho never run\n"),
            ("pkg/Evil.app/Contents/MacOS/Evil", 0o100755, b"#!/bin/sh\necho never run\n"),
            ("pkg/Evil.app/Contents/Info.plist", 0o100644, b"<plist/>"),
            ("pkg/setuid", 0o104755, b"x"),
            ("pkg/readme.txt", 0o100644, b"hello"),
        ]:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            z.writestr(info, data)
    ex.write_quarantine(archive, QUARANTINE_VALUE)
    check(ex.read_quarantine(archive) == QUARANTINE_VALUE, "the fixture archive carries a quarantine attribute")

    result = tool("extract_archive", archive=str(archive))
    out = Path(result.get("path", work / "pkg"))
    check(result.get("ok") and result["file_count"] == 5 and result["quarantined"] is True
          and result["executable_bits_dropped"] == 3, f"extracted: {result.get('message')}")
    every = [out, *sorted(out.rglob("*"))]
    files = [p for p in every if p.is_file()]
    check(all(p.stat().st_mode & 0o7111 == 0 for p in files), "no extracted file has an execute, setuid or setgid bit")
    check(all(p.stat().st_mode & 0o100 for p in every if p.is_dir()), "folders keep search permission")
    check(all(ex.read_quarantine(p) == QUARANTINE_VALUE for p in every),
          f"every extracted file and folder carries the archive's quarantine value ({len(every)} items)")
    xattr = subprocess.run(["/usr/bin/xattr", "-p", "com.apple.quarantine", str(out / "pkg" / "run.command")],
                           capture_output=True, text=True, timeout=10)
    check(xattr.stdout.strip().encode() == QUARANTINE_VALUE, "and /usr/bin/xattr reads the same value back")

    plain = work / "plain.tar.gz"
    with tarfile.open(plain, "w:gz") as t:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("tool.sh")
        info.size, info.mode = len(data), 0o755
        t.addfile(info, __import__("io").BytesIO(data))
    result = tool("extract_archive", archive=str(plain))
    extracted = Path(result["path"]) / "tool.sh"
    check(result["ok"] and result["quarantined"] is False and ex.read_quarantine(extracted) is None
          and extracted.stat().st_mode & 0o111 == 0, "an archive with no quarantine: nothing marked, exec bit still dropped")

    # Failure to mark is a failure, and nothing unmarked is left behind.
    real_write = ex.write_quarantine
    calls = {"n": 0}

    def flaky(path, value):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(1, "simulated")
        real_write(path, value)

    ex.write_quarantine = flaky
    try:
        result = tool("extract_archive", archive=str(archive), destination=str(work / "flaky"))
    finally:
        ex.write_quarantine = real_write
    check(result["ok"] is False and not (work / "flaky").exists(), "if the mark cannot be applied, the extraction is removed")

    # L3: the deadline holds inside one long tar member, not just between members.
    big = work / "big.tar.gz"
    with tarfile.open(big, "w:gz") as t:
        info = tarfile.TarInfo("zeros.bin")
        info.size = 300 * 1024 * 1024
        t.addfile(info, open("/dev/zero", "rb"))
    real_seconds = ex.MAX_SECONDS
    real_bytes = ex.MAX_BYTES
    ex.MAX_SECONDS = 0.05
    ex.MAX_BYTES = 10 ** 12
    try:
        started = time.monotonic()
        result = tool("extract_archive", archive=str(big), destination=str(work / "big"))
        took = time.monotonic() - started
    finally:
        ex.MAX_SECONDS, ex.MAX_BYTES = real_seconds, real_bytes
    check(result["ok"] is False and "longer than" in result["error"] and took < 1.0 and not (work / "big").exists(),
          f"a single 300 MB tar member stops at the deadline ({took:.2f}s): {result['error'][:60]}")

    section("chain D end to end: unzip a download, then try to launch what came out")
    command = out / "pkg" / "run.command"
    bundle = out / "pkg" / "Evil.app"
    launches.clear()
    for label, result in [
        ("open_url(path)", tool("open_url", url=str(command))),
        ("open_url(file URL)", tool("open_url", url=command.as_uri())),
        ("open_url(bundle path)", tool("open_url", url=str(bundle))),
        ("open_app(bundle path)", tool("open_app", name=str(bundle))),
        ("open_app(bundle name)", tool("open_app", name="Evil")),
        ("open_file(run.command)", tool("open_file", path=str(command))),
        ("open_file(Evil.app)", tool("open_file", path=str(bundle))),
        ("open_with(run.command, TextEdit)", tool("open_with", path=str(command), app="TextEdit")),
        ("open_with(Evil.app, Finder)", tool("open_with", path=str(bundle), app="Finder")),
    ]:
        check(result.get("ok") is False, f"{label} refused: {result.get('error', '')[:70]}")
    check(launches == [], "nothing was handed to open")


SECTIONS = {
    "open_url": check_open_url,
    "open_app": check_open_app,
    "extract_archive": check_extract_archive,
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
