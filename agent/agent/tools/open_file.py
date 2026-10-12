"""Open a document or folder in its default app.

`open <path>` is not a safe primitive: for a .command file, a unix
executable, a .jar or an app bundle, "opening" means *running* it -- which
would make this tool run_command with no confirmation. So this is an
allowlist, decided from what macOS itself says the file is and what it would
hand it to (the same LaunchServices data `open` uses), not from a list of
bad extensions:

  * a plain folder opens in Finder;
  * a file opens only if its type is *content* (public.content: documents,
    images, audio, video, text) and is neither a script nor an executable;
  * and only if the app that would receive it is not a terminal or a
    launcher -- which also catches a per-file "always open with Terminal";
  * that app is then named explicitly (`open -a <handler> <file>`), so the
    app that was checked is the app that opens it.

Everything else is refused with a pointer to reveal_in_finder, which shows a
file without opening it. Unknown types are refused rather than guessed at.
"""

from pathlib import Path

from agent.core import bounded

# Injection point: the check script records the argv instead of opening
# windows on the user's screen.
_launch = bounded.run

REFUSED_TYPES = ("public.script", "public.executable")
# Types that are "content" on paper but exist to change the system when
# opened (install a configuration profile, import an automation).
REFUSED_EXACT = {"com.apple.mobileconfig", "com.apple.shortcut"}
# Apps that execute what they are given. Matched on the handler's name.
EXECUTING_HANDLERS = {
    "terminal", "iterm", "iterm2", "warp", "kitty", "alacritty", "ghostty", "wezterm", "hyper", "tabby", "rio",
    "javalauncher", "jar launcher", "python launcher", "installer", "automator installer", "script menu",
    "profilehelper",
}


def resolve_existing(path) -> tuple:
    """(resolved Path, None) or (None, error). Symlinks are followed first so
    every later check is about the real target, not the link's name."""
    if not isinstance(path, str) or not path.strip():
        return None, "no path was given"
    try:
        resolved = Path(path.strip()).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as e:
        return None, f"that path could not be read: {e}"
    if not resolved.exists():
        return None, f"nothing exists at {resolved}"
    return resolved, None


def classify(path: Path) -> dict:
    """{"allowed": True, "handler": app path or None (Finder)} or
    {"allowed": False, "reason": ...}. Read-only LaunchServices queries."""
    from AppKit import NSWorkspace
    from Foundation import NSURL

    workspace = NSWorkspace.sharedWorkspace()
    url = NSURL.fileURLWithPath_(str(path))
    _, uti, _ = url.getResourceValue_forKey_error_(None, "NSURLTypeIdentifierKey", None)
    uti = str(uti or "")

    def conforms(kind: str) -> bool:
        return bool(uti) and bool(workspace.type_conformsToType_(uti, kind))

    if path.suffix.lower() == ".app" or conforms("com.apple.application"):
        return {"allowed": False, "type": uti, "reason": "that is an application; use open_app to launch it"}
    if path.is_dir() and not workspace.isFilePackageAtPath_(str(path)):
        return {"allowed": True, "type": uti, "handler": None}
    if uti in REFUSED_EXACT or any(conforms(kind) for kind in REFUSED_TYPES):
        return {"allowed": False, "type": uti, "reason": "that is a script or program, and opening it could run it"}
    if not conforms("public.content"):
        return {"allowed": False, "type": uti, "reason": "it is not a kind of document this can safely open"}

    handler = workspace.URLForApplicationToOpenURL_(url)
    if handler is None:
        return {"allowed": False, "type": uti, "reason": "no app is set up to open that kind of file"}
    handler_path = str(handler.path())
    name = Path(handler_path).stem.lower()
    if name in EXECUTING_HANDLERS or "terminal" in name:
        return {"allowed": False, "type": uti, "reason": f"it is set to open with {Path(handler_path).stem}, which would run it"}
    return {"allowed": True, "type": uti, "handler": handler_path}


def open_file(path: str) -> dict:
    resolved, error = resolve_existing(path)
    if error:
        return {"ok": False, "error": error}
    try:
        verdict = classify(resolved)
    except Exception as e:
        return {"ok": False, "error": f"could not work out what kind of file that is: {e}"}
    if not verdict["allowed"]:
        return {
            "ok": False,
            "error": f"Not opening {resolved.name}: {verdict['reason']}. reveal_in_finder can show it instead.",
        }

    # The path is resolved, so it is absolute and can never be read as a flag.
    if verdict["handler"]:
        argv = ["open", "-a", verdict["handler"], str(resolved)]
        app = Path(verdict["handler"]).stem
    else:
        argv = ["open", str(resolved)]
        app = "Finder"
    code, _, err = _launch(argv, 10.0)
    if code != 0:
        return {"ok": False, "error": err or f"could not open {resolved.name}"}
    return {"ok": True, "path": str(resolved), "app": app, "message": f"Opened {resolved.name} in {app}."}
