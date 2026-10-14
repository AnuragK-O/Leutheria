"""Open an existing file or folder in a named application (`open -a App path`).

"Safe" in the sense open_app is: it shows something to the user and changes
nothing on disk. The way that stops being true is when "opening" means
"executing" -- a .command double-clicked, a script handed to Terminal, an
installer package. That would make this run_command without the prompt, so
every case where it could happen is refused:

  app     must be a name, never a path to a bundle. Refused: anything on
          ui_access.DENIED_APPS (the apps the agent may never drive) and
          EXECUTING_APPS below -- terminals, script runners, launchers,
          installers, and apps that mount or unpack what they are given.
          Finder is allowed for folders only: Finder "opening" a file is a
          double-click, which launches it with whatever its default is.
  path    symlinks are resolved first, and the checks and the argv use the
          resolved path, so an innocent name can't point at something else.
          Judged by open_file.classify -- the one classifier both tools
          share -- with allow_source: a plain folder, or a file whose type
          macOS calls content and that does not act when opened, or a
          plain source file (.py, .js...) or an undeclared text file
          (Makefile) for the named app to show. Never an app, a script
          macOS runs, a package that is not a document, a location file,
          a macro-enabled document, a playground, a .code-workspace, an
          executable mode, a compiled binary, or anything inside a bundle.
  argv    fixed -- ["open", "-a", app, path] -- no shell.

The app half is a denylist, so it is only as complete as the list. The path
half is what holds if the list misses one: nothing that passes it is
something macOS itself would run.
"""

import subprocess
from pathlib import Path

from agent.core import ui_access
from agent.tools import open_file as classifier

TIMEOUT = 15  # seconds; `open` returns as soon as the app has been asked


def _launch(argv: list) -> subprocess.CompletedProcess:
    """The one place a process is started -- scripts/skills_check_dev.py
    replaces it so the checks never open a real app."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)


# Apps for which "open this" means "run it", "install it", "mount it" or
# "unpack it". Lower-case, no ".app". Extends ui_access.DENIED_APPS rather
# than repeating it; both are checked.
EXECUTING_APPS = {
    # terminals and shells
    "terminal", "iterm", "iterm2", "iterm 2", "warp", "kitty", "alacritty", "ghostty",
    "hyper", "wezterm", "tabby", "rio", "wave", "cool retro term", "xterm", "xquartz",
    # script runners and launchers
    "python launcher", "script editor", "script menu", "applescript utility",
    "automator", "automator application stub", "shortcuts", "jar launcher", "java",
    "wish", "launchpad", "alfred", "raycast",
    # installers, mounters, unpackers
    "installer", "archive utility", "diskimagemounter", "disk utility",
    "the unarchiver", "keka", "ticket viewer", "profile installer", "bluetooth file exchange",
    # other operating systems
    "wine", "crossover", "parallels desktop", "utm", "virtualbox", "vmware fusion",
    "boot camp assistant",
}
FOLDERS_ONLY_APPS = {"finder", "path finder", "forklift"}

def _app_key(app: str) -> str:
    key = " ".join(app.strip().lower().split())
    return key[:-4].strip() if key.endswith(".app") else key


def _refusal(target: Path, folders_only: bool):
    """Why this resolved path must not be opened, or None if it may be."""
    blocked = classifier.risky_suffix(target)
    if blocked:
        return f"{blocked} items are run, installed or mounted rather than opened, so this is refused"
    if folders_only and not target.is_dir():
        return "that application opens folders only; name the application that should show the file"
    verdict = classifier.classify(target, allow_source=True)
    if not verdict["allowed"]:
        return f"{target.name} is refused: {verdict['reason']}"
    return None


def open_with(path: str, app: str) -> dict:
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "no file or folder given"}
    if not isinstance(app, str) or not app.strip():
        return {"ok": False, "error": "no application named"}
    app = app.strip()
    if "/" in app or app.startswith("-") or len(app) > 100 or any(ord(c) < 32 for c in app):
        return {"ok": False, "error": "give the application by name, for example Visual Studio Code"}
    key = _app_key(app)
    if ui_access.is_denied(key) or key in EXECUTING_APPS:
        return {"ok": False, "error": f"{app} can never be handed a file by Leutheria"}

    try:
        target = Path(path.strip()).expanduser().resolve()
    except (OSError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}
    if not target.exists():
        return {"ok": False, "error": f"{target} does not exist"}
    reason = _refusal(target, key in FOLDERS_ONLY_APPS)
    if reason:
        return {"ok": False, "error": reason}

    try:
        # The resolved path is absolute, so it can't be read as an option.
        result = _launch(["open", "-a", app, str(target)])
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{app} did not respond within {TIMEOUT} seconds"}
    if result.returncode != 0:
        err = result.stderr.strip()
        if "unable to find application" in err.lower():
            return {"ok": False, "error": f"there is no application named {app}"}
        return {"ok": False, "error": err or f"could not open {target} with {app}"}
    return {"ok": True, "path": str(target), "app": app, "message": f"opened {target} in {app}"}
