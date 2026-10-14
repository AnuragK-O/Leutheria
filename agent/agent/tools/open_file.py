"""Open a document or folder in its default app -- and the one file classifier
that open_with shares.

`open <path>` is not a safe primitive: for a .command file, a unix
executable, a .jar or an app bundle, "opening" means *running* it -- which
would make this tool run_command with no confirmation. So this is an
allowlist, decided from what macOS itself says the file is and what it would
hand it to (the same LaunchServices data `open` uses), not from a list of
bad extensions:

  * a plain folder opens in Finder;
  * a file opens only if its type is *content* (public.content: documents,
    images, audio, video, text) and is neither a script nor an executable,
    nor one of the content types that act when opened (REFUSED_EXACT);
  * it must not be executable by its mode, a compiled binary, or a script
    with no extension, whatever its type says;
  * and only if the app that would receive it is not a terminal or a
    launcher -- which also catches a per-file "always open with Terminal";
  * that app is then named explicitly (`open -a <handler> <file>`), so the
    app that was checked is the app that opens it.

RISKY_SUFFIXES is a backstop behind all that, checked on the file and every
folder above it: it matters for types macOS has no declaration for (a
.code-workspace is "dyn.<hash>" to LaunchServices but special to VS Code).

open_with uses classify(path, allow_source=True): the same rules, plus two
allowances for a NAMED app that is not a runner (open_with refuses those):
plain source files (SOURCE_SUFFIXES: .py, .js, ... -- "scripts" to macOS,
text to an editor) and text files of a type nothing on the Mac declares
(Makefile, .gitignore). Shell scripts stay refused: macOS runs those.

Everything refused points at reveal_in_finder, which shows a file without
opening it. Unknown types are refused rather than guessed at.
"""

import stat
from pathlib import Path

from agent.core import bounded

# Injection point: the check script records the argv instead of opening
# windows on the user's screen.
_launch = bounded.run

REFUSED_TYPES = ("public.script", "public.executable")
# Types that are "content" on paper but act when opened: install a profile,
# import or run an automation, run code in a page or a playground, install
# an extension, start Java Web Start.
REFUSED_EXACT = {
    "com.apple.mobileconfig",
    "com.apple.shortcut",
    "com.apple.shortcuts.workflow-file",  # .wflow: imported into Shortcuts
    "com.apple.webarchive",  # .webarchive: a saved page with its scripts, run as a local file
    "com.apple.dt.playground",  # .playground: Xcode runs it on open
    "com.apple.dt.swiftpm-playground",
    "com.apple.swift-playgrounds.playgroundbook",
    "org.chromium.extension",  # .crx
    "com.apple.safari.extension",
    "com.sun.java-web-start",  # .jnlp
}
# Macro-enabled Office documents (.docm, .xlsm, .pptm and their templates).
_REFUSED_UTI_WORDS = ("macroenabled",)

# Apps that execute what they are given. Matched on the handler's name.
EXECUTING_HANDLERS = {
    "terminal", "iterm", "iterm2", "warp", "kitty", "alacritty", "ghostty", "wezterm", "hyper", "tabby", "rio",
    "javalauncher", "jar launcher", "python launcher", "installer", "automator installer", "script menu",
    "profilehelper", "shortcuts", "script editor", "archive utility", "diskimagemounter",
}

# File and bundle types macOS (or a common app) executes, installs, mounts,
# follows or auto-runs. Lower-case, with the dot.
RISKY_SUFFIXES = {
    # programs, scripts, bundles
    ".app", ".command", ".tool", ".terminal", ".sh", ".bash", ".zsh", ".csh", ".ksh",
    ".fish", ".workflow", ".action", ".scpt", ".scptd", ".applescript", ".jxa", ".jar",
    ".prefpane", ".osax", ".qlgenerator", ".saver", ".kext", ".plugin", ".bundle",
    ".framework", ".xpc", ".appex", ".dylib", ".so", ".exe", ".bat", ".cmd", ".ps1", ".vbs",
    ".scr", ".msi", ".lnk", ".service", ".alfredworkflow", ".wdgt", ".mdimporter", ".appimage",
    # installers, disk images, configuration
    ".pkg", ".mpkg", ".dmg", ".iso", ".sparseimage", ".sparsebundle", ".deb", ".rpm",
    ".shortcut", ".wflow", ".mobileconfig", ".mobileprovision",
    # locations: opening one follows it -- to any URL scheme, a share, a VNC session
    ".webloc", ".inetloc", ".fileloc", ".url", ".afploc", ".vncloc", ".smbloc", ".ftploc",
    ".telnetloc", ".newsloc", ".mailloc", ".desktop", ".rdp", ".ica", ".jnlp",
    # documents that run code when opened
    ".docm", ".dotm", ".xlsm", ".xltm", ".xlam", ".pptm", ".potm", ".ppsm", ".ppam", ".sldm",
    ".webarchive", ".playground", ".playgroundbook", ".swiftpm",
    ".code-workspace",  # VS Code: tasks with runOn folderOpen
    # extensions
    ".crx", ".safariextz", ".xpi", ".vsix",
    # Safari's in-progress download package
    ".download",
}

# Source files an editor shows as text. macOS calls most of them scripts.
# No shell scripts: those it runs.
SOURCE_SUFFIXES = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rb", ".pl", ".pm", ".php", ".lua",
    ".r", ".swift", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cc", ".m", ".mm", ".java", ".kt",
    ".kts", ".scala", ".sql", ".css", ".scss", ".less", ".md", ".txt", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".xml", ".html", ".htm", ".vue", ".svelte", ".dart", ".ex", ".exs",
    ".erl", ".hs", ".ml", ".clj", ".groovy", ".gradle", ".cs", ".fs", ".zig", ".nim", ".tf",
}
# Build files known by name, which macOS types as make source and so on.
SOURCE_NAMES = {
    "makefile", "gnumakefile", "dockerfile", "containerfile", "rakefile", "gemfile", "podfile",
    "brewfile", "justfile", "procfile", "vagrantfile", "jenkinsfile", "cmakelists.txt",
}
# Types LaunchServices assigns to a file it knows nothing about.
_UNDECLARED = ("public.data", "public.item")

# Mach-O (thin, both byte orders, and fat) and ELF.
_BINARY_MAGIC = (
    b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\x7fELF",
)


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


def risky_suffix(path: Path):
    """The first RISKY_SUFFIXES suffix on the path or any folder above it."""
    return next((p.suffix.lower() for p in (path, *path.parents) if p.suffix.lower() in RISKY_SUFFIXES), None)


def _head(path: Path, size: int) -> bytes:
    with path.open("rb") as f:
        return f.read(size)


def _looks_like_text(path: Path) -> bool:
    try:
        head = _head(path, 8192)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        return e.start >= len(head) - 4  # a character cut off at the end of the sample
    return True


def _refuse(uti: str, reason: str) -> dict:
    return {"allowed": False, "type": uti, "reason": reason}


def classify(path: Path, allow_source: bool = False) -> dict:
    """{"allowed": True, "handler": app path or None, "type": uti} or
    {"allowed": False, "reason": ...} for a resolved path. Read-only
    LaunchServices queries. allow_source: open_with's allowances (above)."""
    from AppKit import NSWorkspace
    from Foundation import NSURL

    workspace = NSWorkspace.sharedWorkspace()
    url = NSURL.fileURLWithPath_(str(path))
    _, uti, _ = url.getResourceValue_forKey_error_(None, "NSURLTypeIdentifierKey", None)
    uti = str(uti or "")

    def conforms(kind: str) -> bool:
        return bool(uti) and bool(workspace.type_conformsToType_(uti, kind))

    if path.suffix.lower() == ".app" or conforms("com.apple.application"):
        return _refuse(uti, "that is an application; use open_app to launch it")
    try:
        mode = path.stat().st_mode
    except OSError as e:
        return _refuse(uti, f"it could not be read: {e}")
    is_dir = stat.S_ISDIR(mode)
    package = is_dir and bool(workspace.isFilePackageAtPath_(str(path)))
    suffix = risky_suffix(path)

    if is_dir and not package:
        if suffix:
            return _refuse(uti, f"it is inside a {suffix} item, which is run or installed rather than opened")
        return {"allowed": True, "type": uti, "handler": None}
    if not is_dir:
        if not stat.S_ISREG(mode):
            return _refuse(uti, "it is not a regular file or a folder")
        if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            return _refuse(uti, f"{path.name} is an executable file: a script or program that would run")
        try:
            head = _head(path, 4)
        except OSError as e:
            return _refuse(uti, f"it could not be read: {e}")
        if head.startswith(_BINARY_MAGIC):
            return _refuse(uti, f"{path.name} is a compiled program")
        if head.startswith(b"#!") and not path.suffix:
            return _refuse(uti, f"{path.name} is a script with no file extension, so it could run")

    if uti in REFUSED_EXACT or any(word in uti.lower() for word in _REFUSED_UTI_WORDS):
        return _refuse(uti, "that is a script or program, or a document that runs code when opened")
    source = allow_source and not is_dir and (
        path.suffix.lower() in SOURCE_SUFFIXES or path.name.lower() in SOURCE_NAMES
    )
    if any(conforms(kind) for kind in REFUSED_TYPES) and not source:
        return _refuse(uti, "that is a script or program, and opening it could run it")
    if not conforms("public.content") and not source:
        undeclared = uti.startswith("dyn.") or uti in _UNDECLARED
        if not (allow_source and not is_dir and undeclared and _looks_like_text(path)):
            return _refuse(uti, "it is not a kind of document this can safely open")
    if suffix:
        return _refuse(uti, f"{suffix} items are run, installed or mounted rather than opened")
    # "handler" is filled in by open_file (_handler); open_with names its own app.
    return {"allowed": True, "type": uti, "handler": "default"}


def _handler(path: Path) -> tuple:
    """(handler app path, None) or (None, why not)."""
    from AppKit import NSWorkspace
    from Foundation import NSURL

    handler = NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(NSURL.fileURLWithPath_(str(path)))
    if handler is None:
        return None, "no app is set up to open that kind of file"
    handler_path = str(handler.path())
    name = Path(handler_path).stem.lower()
    if name in EXECUTING_HANDLERS or "terminal" in name:
        return None, f"it is set to open with {Path(handler_path).stem}, which would run it"
    return handler_path, None


def open_file(path: str) -> dict:
    resolved, error = resolve_existing(path)
    if error:
        return {"ok": False, "error": error}
    try:
        verdict = classify(resolved)
        if verdict["allowed"] and verdict["handler"]:
            verdict["handler"], why = _handler(resolved)
            if why:
                verdict = {"allowed": False, "reason": why}
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
