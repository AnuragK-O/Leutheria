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


# --- git_status ---------------------------------------------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=30)


def hostile_repo(where: Path, marker_dir: Path) -> Path:
    """A repository whose own config names programs at every hook git offers
    a read-only caller, each of which would touch a marker file if run."""
    repo = where
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.dat").write_text("two\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")

    def marker(name):
        return f"touch {marker_dir / name}"

    included = repo / "extra.gitconfig"
    included.write_text(f'[filter "inc.dotted"]\n\tclean = {marker("include_clean")}\n')
    attributes = "* filter=x\n*.dat filter=inc.dotted diff=tc\n"
    (repo / ".gitattributes").write_text(attributes)
    hooks = repo / "hooks"
    hooks.mkdir()
    for hook in ("post-index-change", "pre-commit", "post-checkout", "reference-transaction"):
        (hooks / hook).write_text(f"#!/bin/sh\n{marker('hook_' + hook)}\n")
        (hooks / hook).chmod(0o755)
    config = [
        ("filter.x.clean", marker("filter_clean")),
        ("filter.x.smudge", marker("filter_smudge")),
        ("filter.x.process", marker("filter_process")),
        ("filter.x.required", "true"),
        ("core.fsmonitor", marker("fsmonitor")),
        ("core.hooksPath", str(hooks)),
        ("core.pager", marker("pager")),
        ("pager.status", marker("pager_status")),
        ("pager.log", marker("pager_log")),
        ("core.editor", marker("editor")),
        ("core.sshCommand", marker("ssh")),
        ("core.askPass", marker("askpass")),
        ("credential.helper", "!" + marker("credential")),
        ("diff.external", marker("diff_external")),
        ("diff.tc.textconv", marker("textconv")),
        ("gpg.program", marker("gpg")),
        ("log.showSignature", "true"),
        ("alias.status", "!" + marker("alias")),
        ("alias.log", "!" + marker("alias_log")),
        ("include.path", str(included)),
        ("remote.origin.url", "https://example.invalid/r.git"),
        ("remote.origin.uploadpack", marker("uploadpack")),
        ("remote.origin.receivepack", marker("receivepack")),
        ("core.untrackedCache", "true"),
    ]
    for key, value in config:
        _git(repo, "config", key, value)
    # Every tracked file stat-dirty, as after unzipping a project: status has
    # to re-hash them, which is when the clean filter runs.
    time.sleep(1.1)
    for path in (repo / "a.txt", repo / "sub" / "b.dat"):
        path.write_text(path.read_text())
    return repo


async def check_git_status():
    from agent.tools import git_status as gs

    section("git_status runs nothing the repository chooses (H3, chain F)")
    home = TMP / "git_home"
    home.mkdir()
    saved = {k: os.environ.get(k) for k in ("HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
    os.environ.update({"HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"})
    try:
        markers = TMP / "git_markers"
        markers.mkdir()
        repo = hostile_repo(TMP / "git_repo", markers)

        # The PoC, unneutralised: plain `git status` in this repo does run the filter.
        subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, timeout=30,
                       env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        check((markers / "filter_process").exists() or (markers / "filter_clean").exists(),
              f"the PoC is live: bare git status ran the repository's filter {sorted(p.name for p in markers.iterdir())}")
        for p in markers.iterdir():
            p.unlink()
        time.sleep(1.1)
        for path in (repo / "a.txt", repo / "sub" / "b.dat"):
            path.write_text(path.read_text())

        result, asked = await through_dispatch("git_status", {"path": str(repo), "log_count": 5})
        ran = sorted(p.name for p in markers.iterdir())
        check(result.get("ok") is True and asked == [], f"git_status reads the repository: {result.get('message', result.get('error'))}")
        check(ran == [], f"and nothing the repository named was run: {ran}")
        check(result["branch"] == "main" and result["commits"] and result["commits"][0]["subject"] == "first",
              "branch and commits are still read")
        check(result["remote_url"] == "https://example.invalid/r.git", "the remote URL is still read")

        # And with submodules: a submodule's own filter is not run either.
        inner = hostile_repo(TMP / "git_inner", markers)
        outer = TMP / "git_outer"
        outer.mkdir()
        _git(outer, "init", "-q", "-b", "main")
        _git(outer, "config", "user.email", "t@example.invalid")
        _git(outer, "config", "user.name", "t")
        _git(outer, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "inner")
        _git(outer, "commit", "-q", "-m", "with submodule")
        for p in markers.iterdir():
            p.unlink()
        sub_config = outer / ".git" / "modules" / "inner" / "config"
        with open(sub_config, "a") as f:
            f.write(f'[filter "x"]\n\tclean = touch {markers / "submodule_clean"}\n'
                    f'\tprocess = touch {markers / "submodule_process"}\n'
                    f'[core]\n\tfsmonitor = touch {markers / "submodule_fsmonitor"}\n')
        time.sleep(1.1)
        (outer / "inner" / "a.txt").write_text("one\n")
        result, _ = await through_dispatch("git_status", {"path": str(outer)})
        check(result.get("ok") is True and sorted(p.name for p in markers.iterdir()) == [],
              f"a submodule's filters are not run either: {sorted(p.name for p in markers.iterdir())}")

        # An ordinary repository is unaffected.
        plain = TMP / "git_plain"
        plain.mkdir()
        _git(plain, "init", "-q", "-b", "trunk")
        (plain / "x.txt").write_text("x")
        result, _ = await through_dispatch("git_status", {"path": str(plain)})
        check(result.get("ok") and result["branch"] == "trunk" and result["untracked"] == 1, "an ordinary repository reads as before")
        check(gs._neutralising_args(plain) == [], "and needs no filter overrides")
        check(gs._neutralising_args(repo)[:2] == ["-c", "filter.inc.dotted.clean="] or
              "filter.inc.dotted.clean=" in gs._neutralising_args(repo),
              "a dotted filter name pulled in by include.path is neutralised too")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --- fetch_url ----------------------------------------------------------------

class SlowServer:
    """A raw TCP server on 127.0.0.1 that misbehaves in one of several ways
    and records when the client hung up."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        self.client_gone_at = None
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.listener.accept()
        conn.settimeout(0.2)
        try:
            conn.recv(65536)  # the request (or a TLS ClientHello)
        except OSError:
            pass
        lines = []
        if self.behaviour == "drip":
            lines = [b"HTTP/1.1 200 OK\r\n"] + [b"X-Drip-%d: x\r\n" % i for i in range(60)]
        elif self.behaviour == "stall":
            lines = [b"HTTP/1.1 200 OK\r\n"]
        for line in lines:
            try:
                conn.sendall(line)
            except OSError:
                break
            if self._wait_for_hangup(conn, 1.0 if self.behaviour == "drip" else 0.0):
                break
        while not self.stop.is_set() and self.client_gone_at is None:
            self._wait_for_hangup(conn, 0.2)
        conn.close()

    def _wait_for_hangup(self, conn, seconds):
        end = time.monotonic() + seconds
        while True:
            try:
                if conn.recv(1) == b"":
                    self.client_gone_at = time.monotonic()
                    return True
            except socket.timeout:
                pass
            except OSError:
                self.client_gone_at = time.monotonic()
                return True
            if time.monotonic() >= end:
                return False

    def close(self):
        self.stop.set()
        self.listener.close()


def check_fetch_url():
    from agent.tools import fetch_url as fu

    section("fetch_url: one absolute deadline over every phase (M2)")
    real = (fu.TIMEOUT, fu.DEADLINE, fu._address_allowed)
    fu._address_allowed = lambda address, port: address == "127.0.0.1"  # this script's servers only
    try:
        cases = [
            # (behaviour, scheme, TIMEOUT, DEADLINE, what)
            ("drip", "http", 2.0, 3.0, "headers dripped one line a second, each inside the per-read timeout"),
            ("stall", "http", 60.0, 2.0, "a status line, then nothing"),
            ("silent", "https", 60.0, 2.0, "a TLS handshake that never gets an answer"),
            ("silent", "http", 60.0, 2.0, "no response at all"),
        ]
        for behaviour, scheme, timeout, deadline, what in cases:
            fu.TIMEOUT, fu.DEADLINE = timeout, deadline
            server = SlowServer(behaviour)
            started = time.monotonic()
            result = tool("fetch_url", url=f"{scheme}://127.0.0.1:{server.port}/")
            took = time.monotonic() - started
            time.sleep(0.5)
            gone = server.client_gone_at is not None and server.client_gone_at - started < deadline + 0.6
            server.close()
            check(result["ok"] is False and "timed out" in result["error"] and took < deadline + 0.5,
                  f"{what}: stopped at {took:.1f}s against a {deadline:g}s deadline ({result['error']})")
            check(gone, f"and the connection was closed, not left open ({what})")
        stray = [t.name for t in threading.enumerate() if t.name.startswith("Thread") and t.is_alive() and "Timer" in repr(t)]
        check(stray == [], f"no watchdog timer is left running: {stray}")
    finally:
        fu.TIMEOUT, fu.DEADLINE, fu._address_allowed = real

    section("fetch_url: addresses that embed a private IPv4 address (L5)")
    allowed = fu._address_allowed
    for address in [
        "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::c0a8:101", "64:ff9b::a9fe:a9fe", "64:ff9b::6440:1",
        "64:ff9b:1::808:808", "64:ff9b:1::7f00:1", "::ffff:127.0.0.1", "::127.0.0.1", "::a00:1",
        "2002:7f00:1::1", "2002:a00:1::1", "127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fe80::1",
    ]:
        check(not allowed(address, 443), f"refuses {address}")
    for address in ["64:ff9b::808:808", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"]:
        check(allowed(address, 443), f"allows {address}")


# --- opener gaps --------------------------------------------------------------

def check_openers():
    section("open_file / open_with: one classifier, the review's gaps closed (L1)")
    base = TMP / "openers"
    base.mkdir()

    def make(name, body="hello\n", folder=False):
        target = base / name
        if folder:
            target.mkdir()
            (target / "Contents").mkdir()
            (target / "Contents" / "Info.plist").write_text("<plist/>")
        else:
            target.write_text(body)
        return target

    plist = '<?xml version="1.0"?><plist version="1.0"><dict><key>URL</key><string>vnc://x</string></dict></plist>\n'
    both_refused = [
        make("flow.wflow"), make("page.webarchive"), make("Play.playground", folder=True), make("ext.crx"),
        make("report.docm"), make("sheet.xlsm"), make("deck.pptm"), make("tpl.dotm"), make("web.jnlp"),
        make("share.afploc", plist), make("screen.vncloc", plist), make("proj.code-workspace", '{"folders":[]}\n'),
        make("Widget.wdgt", folder=True), make("Spot.mdimporter", folder=True), make("file.zip.download", folder=True),
        make("remote.rdp"), make("run.command"), make("build.sh"), make("addin.xlam"), make("Book.swiftpm", folder=True),
    ]
    for target in both_refused:
        launches.clear()
        opened = tool("open_file", path=str(target))
        check(opened["ok"] is False and launches == [], f"open_file refuses {target.name}: {opened['error'][:60]}")
        for app in ("Visual Studio Code", "TextEdit", "Finder", "Xcode"):
            result = tool("open_with", path=str(target), app=app)
            check(result["ok"] is False and launches == [], f"open_with refuses {target.name} into {app}")
    # Symlinks are judged by what they point at.
    for link_name, points_at in [("notes.txt", "flow.wflow"), ("page.html", "page.webarchive"), ("Folder", "Widget.wdgt"),
                                 ("settings.json", "proj.code-workspace"), ("doc.docx", "report.docm")]:
        link = base / link_name
        link.symlink_to(base / points_at)
        check(tool("open_file", path=str(link))["ok"] is False and tool("open_with", path=str(link), app="Visual Studio Code")["ok"] is False
              and launches == [], f"both refuse {link_name}, a symlink to {points_at}")
    inner = base / "Widget.wdgt" / "Contents" / "Info.plist"
    check(tool("open_with", path=str(inner), app="TextEdit")["ok"] is False, "a file inside a package is refused")

    # What must keep working.
    folder = base / "project"
    folder.mkdir()
    for app in ("Visual Studio Code", "Finder"):
        launches.clear()
        result = tool("open_with", path=str(folder), app=app)
        check(result["ok"] and launches == [["open", "-a", app, str(folder)]], f"a folder still opens in {app}")
    for name, body in [("main.py", "print(1)\n"), ("index.js", "x\n"), ("Makefile", "all:\n\ttrue\n"), (".gitignore", "x\n"),
                       ("Dockerfile", "FROM x\n"), ("page.html", "<p>x</p>\n"), ("notes.md", "# x\n"), ("data.json", "{}\n"),
                       ("conf.toml", "a = 1\n"), ("app.ts", "let a = 1\n")]:
        target = base / "project" / name
        target.write_text(body)
        launches.clear()
        result = tool("open_with", path=str(target), app="Visual Studio Code")
        check(result["ok"] and len(launches) == 1, f"open_with still opens {name} in an editor")
    launches.clear()
    result = tool("open_file", path=str(base / "project" / "page.html"))
    check(result["ok"] and launches and launches[0][1] == "-a", "open_file still opens an .html page in the browser")
    binary = base / "project" / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02binary")
    check(tool("open_with", path=str(binary), app="Visual Studio Code")["ok"] is False, "an undeclared binary file is still refused")
    script = base / "project" / "tool.py"
    script.write_text("print(1)\n")
    check(tool("open_file", path=str(script))["ok"] is False, "open_file still refuses a .py (its default app could be a runner)")


# --- a missing folder, said plainly (QA) ----------------------------------------

async def check_missing_folder():
    section("bulk skills on a folder that doesn't exist (QA)")
    # The path policy (security A) refuses bulk reorganising outside home; a temp
    # folder is a registered scratch root for this check only, as in the other checks.
    from agent.core import path_policy
    if str(TMP) not in path_policy.EXTRA_FREE_ROOTS:
        path_policy.EXTRA_FREE_ROOTS.append(str(TMP))
    missing = TMP / "no such folder"
    for skill in ("organize_folder", "organize_by_date", "find_large_files", "folder_summary"):
        result, asked = await through_dispatch(skill, {"path": str(missing)})
        error = result.get("error", "")
        check(result["ok"] is False and "doesn't exist" in error and "find_files" not in error and "failed while" not in error,
              f"{skill}: {error[:90]}")
    there = TMP / "a file"
    there.write_text("x")
    result, _ = await through_dispatch("organize_folder", {"path": str(there)})
    check(result["ok"] is False and "doesn't exist" not in result["error"], f"a file that exists is not called missing: {result['error'][:80]}")


# --- GUI control: no terminal is drivable (QA, registry load test) ----------------

async def check_gui_denied():
    section("GUI control: every terminal, runner, launcher and VM is denied")
    from agent.core import ui_access
    from agent.tools import open_with as open_with_mod
    # Typing into any terminal is run_command with no prompt; the openers already
    # know every terminal and runner, so the GUI denylist must cover them too.
    executing = {name for name in open_file_mod.EXECUTING_HANDLERS
                 if name not in ("javalauncher", "jar launcher", "installer", "automator installer",
                                 "script menu", "profilehelper", "archive utility", "diskimagemounter")}
    executing |= {"warp", "kitty", "alacritty", "ghostty", "hyper", "wezterm", "tabby", "rio", "wave",
                  "cool retro term", "xterm", "xquartz", "automator", "shortcuts", "alfred", "raycast",
                  "utm", "parallels desktop", "virtualbox", "vmware fusion"}
    check(executing <= open_with_mod.EXECUTING_APPS | open_file_mod.EXECUTING_HANDLERS,
          "every app the GUI denylist must cover is one the openers already refuse")
    missing = sorted(name for name in executing if not ui_access.is_denied(name))
    check(not missing, f"GUI denylist covers every terminal/runner/launcher/VM (missing: {missing})")
    for shown in ("Ghostty", " Warp ", "WezTerm", "kitty", "iTerm2", "Raycast", "UTM"):
        result, asked = await through_dispatch("focus_app", {"name": shown})
        check(result["ok"] is False and "never be controlled" in result.get("error", "") and not asked,
              f"focus_app {shown.strip()!r} refused with no prompt")
    check(not ui_access.is_denied("TextEdit"), "an ordinary app is not denied")

    # The denylist is judged on the app a name resolves to: resolve_app's prefix and
    # substring passes must not carry "Keych" to Keychain Access or "Ghost" to Ghostty
    # (QA round 2). Every AppKit/AX call is stubbed; nothing real is read or launched.
    running = {"Keychain Access": 9101, "Ghostty": 9102, "TextEdit": 9103, "VirtualBox VM": 9104}
    saved = {k: getattr(ui_access, k) for k in ("running_apps", "ensure_trusted", "subprocess")}
    launched = []

    class _NoLaunch:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(argv, **kw):
            launched.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "stubbed")

    ui_access.running_apps = lambda: dict(running)
    ui_access.ensure_trusted = lambda: None
    ui_access.subprocess = _NoLaunch
    try:
        check(ui_access.resolve_app("Text") == ("TextEdit", 9103), "a partial name still resolves an ordinary app")
        for partial in ("Keych", "chain", "Ghost", "VirtualBox"):
            try:
                ui_access.resolve_app(partial)
                refused = False
            except ui_access.DeniedAppError:
                refused = True
            check(refused, f"resolve_app({partial!r}) refuses the denied app it would match")
        result = tool("describe_ui", app="Keych")
        check(result["ok"] is False and "never" in result["error"], f"describe_ui 'Keych' reads nothing: {result.get('error', '')[:60]}")
        result = tool("focus_app", name="Ghost")
        check(result["ok"] is False and "never" in result["error"] and not launched,
              f"focus_app 'Ghost' is refused and launches nothing: {result.get('error', '')[:60]}")
    finally:
        for k, v in saved.items():
            setattr(ui_access, k, v)


SECTIONS = {
    "open_url": check_open_url,
    "open_app": check_open_app,
    "extract_archive": check_extract_archive,
    "git_status": check_git_status,
    "fetch_url": check_fetch_url,
    "openers": check_openers,
    "missing_folder": check_missing_folder,
    "gui_denied": check_gui_denied,
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
