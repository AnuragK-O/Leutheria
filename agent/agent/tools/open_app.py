"""Launch an installed application, by name.

`open -a <anything>` is not a safe primitive: handed a path it launches
whatever bundle is there -- one that was unzipped into Downloads a moment
ago, say -- which would make this run_command with no confirmation. So the
argument is a NAME and never reaches `open` itself. It is looked up in the
folders where applications are installed, and what `open` is given is the
bundle path found there:

  name     no "/", no "..", no leading "-", no control characters. It is
           only ever compared against directory listings, so there is
           nothing for a path trick to act on; these are refused outright
           to give a plain reason.
  lookup   a case-insensitive match on the bundle's file name (with or
           without ".app", spaces optional) or, failing that, on the name
           the bundle gives itself (CFBundleDisplayName / CFBundleName), in
           APP_ROOTS and the plain folders directly inside them
           (Utilities, a vendor's folder). Read off the filesystem rather
           than asked of NSWorkspace/LaunchServices: LaunchServices will
           happily resolve a name to a bundle sitting in Downloads, and
           NSWorkspace state is stale in a process with no run loop.
  symlink  the bundle's real path must itself be inside one of the roots
           (/Applications/Safari.app is a link into the system cryptex,
           which is one), so a link planted in ~/Applications cannot point
           the name at a bundle somewhere else.
  argv     ["open", "-a", bundle] and nothing after it: this tool takes
           one argument, so there is no way to hand the app a document or
           a script. open_with and open_file are the tools that do that,
           and they have their own rules.

Launching an app on ui_access.DENIED_APPS (Terminal, say) is allowed:
an empty Terminal window is harmless, and typing into it is what the
scoped tier refuses.
"""

import os
import plistlib
from pathlib import Path

from agent.core import bounded

TIMEOUT = 15.0  # `open` returns once the app has been asked to launch
MAX_NAME = 100

# Injection point: the check scripts record the argv instead of launching.
_launch = bounded.run

# Where an installed application can live, in the order a name is looked
# up. The system's folders come first and the user-writable one last, so
# nothing dropped into ~/Applications can take the name of a system app.
_SYSTEM_ROOTS = (
    "/Applications",
    "/System/Applications",
    "/System/Applications/Utilities",
    "/Applications/Utilities",
    "/System/Library/CoreServices",  # Finder, Siri, Screen Sharing
    "/System/Library/CoreServices/Applications",  # Archive Utility, Keychain Access
    "/System/Cryptexes/App/System/Applications",  # where Safari really is
)


def app_roots() -> list:
    return [Path(p) for p in _SYSTEM_ROOTS] + [Path.home() / "Applications"]


def _key(name: str) -> str:
    key = " ".join(name.casefold().split())
    return key[:-4].rstrip() if key.endswith(".app") else key


def _loose(key: str) -> str:
    return key.replace(" ", "")


def _bundles(roots: list):
    """Every .app directly in a root, or directly in a plain folder inside
    one. Yields each path once, in root order."""
    seen = set()
    for root in roots:
        for folder in (root, None):
            if folder is None:
                try:
                    folders = sorted(p for p in root.iterdir() if p.suffix.lower() != ".app" and p.is_dir())
                except OSError:
                    continue
            else:
                folders = [folder]
            for place in folders:
                try:
                    entries = sorted(place.iterdir())
                except OSError:
                    continue
                for entry in entries:
                    if entry.suffix.lower() == ".app" and str(entry) not in seen:
                        seen.add(str(entry))
                        yield entry


def _own_names(bundle: Path) -> set:
    """The names a bundle gives itself, for 'Voice Memos' -> VoiceMemos.app."""
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as f:
            info = plistlib.load(f)
    except Exception:
        return set()
    return {_key(v) for k in ("CFBundleDisplayName", "CFBundleName") if isinstance(v := info.get(k), str) and v.strip()}


def _inside_roots(bundle: Path, roots: list) -> bool:
    try:
        real = os.path.realpath(bundle)
        if not os.path.isdir(real) or not real.lower().endswith(".app"):
            return False
    except OSError:
        return False
    for root in roots:
        base = os.path.realpath(root)
        relative = os.path.relpath(real, base)
        # Directly in the root, or one plain folder down. Never deeper: an
        # app inside another app's bundle is not an installed application.
        parts = Path(relative).parts
        if parts[0] != ".." and len(parts) <= 2 and not any(p.lower().endswith(".app") for p in parts[:-1]):
            return True
    return False


def resolve_app(name) -> tuple:
    """(bundle path as str, None) or (None, why it is refused)."""
    if not isinstance(name, str) or not name.strip():
        return None, "no application was named"
    name = name.strip()
    if "/" in name or "\\" in name or ".." in name or name.startswith(("-", "~", ".")) or ":" in name:
        return None, (
            "open_app takes the name of an installed application, for example Safari, never a path "
            "or an option. Use open_file for a document or folder."
        )
    if len(name) > MAX_NAME:
        return None, "that is too long to be an application's name"
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return None, "an application's name cannot contain control characters"

    roots = app_roots()
    wanted = _key(name)
    candidates = list(_bundles(roots))
    passes = (
        lambda b: _key(b.name) == wanted,
        lambda b: _loose(_key(b.name)) == _loose(wanted),
        lambda b: wanted in _own_names(b),
    )
    for matches in passes:
        for bundle in candidates:
            if matches(bundle) and _inside_roots(bundle, roots):
                return str(bundle), None
    return None, (
        f"there is no installed application called {name}. open_app only launches applications in the "
        "Applications folders; list_running_apps and find_files can show what is there."
    )


def open_app(name: str) -> dict:
    bundle, error = resolve_app(name)
    if error:
        return {"ok": False, "error": error}
    # One argument, by construction: nothing follows the bundle path.
    code, _, stderr = _launch(["open", "-a", bundle], TIMEOUT)
    shown = Path(bundle).stem
    if code != 0:
        return {"ok": False, "error": stderr or f"could not open {shown}"}
    return {"ok": True, "app": shown, "path": bundle, "message": f"opened {shown}"}
