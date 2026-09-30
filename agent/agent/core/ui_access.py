"""macOS GUI actuation primitives: app focus, Accessibility tree reads, and
synthetic keystrokes.

This is the first thing in the codebase that isn't safe by construction. A
shell tool's blast radius is bounded by its parameters; `type_text` types
into whatever happens to be frontmost, and the Accessibility API can read
any window on the machine. Two structural mitigations live here rather than
in the calling tools, so nothing can route around them:

  DENIED_APPS  -- apps that can never be driven or read, grant or no grant.
                  Terminal is on it because typing into a shell is a trivial
                  bypass of the entire confirmation model: `type_text` would
                  become `run_command` with no prompt.
  redaction    -- AXSecureTextField values are never returned by describe().
                  That's what lets describe_ui stay a "safe" tool: reading a
                  UI tree is how the model finds where to type, and making it
                  ask permission first would break the look-then-act loop the
                  way it would if list_files needed approval.

Session-scoped grants (which app may be driven, and until when) are held in
session.py and enforced in dispatcher.py -- not here.
"""

import subprocess
import time

from AppKit import NSRunningApplication, NSWorkspace
from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementGetPid,
    AXUIElementSetMessagingTimeout,
)
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventSetFlags,
    kCGHIDEventTap,
)

# Never drivable, never readable, however the request is phrased and whatever
# grants are live. Matched case-insensitively against the resolved app name.
DENIED_APPS = {
    "terminal",
    "iterm",
    "iterm2",
    "keychain access",
    "system settings",
    "system preferences",
    "console",
    "script editor",
    "1password",
    "1password 7",
    "bitwarden",
    "activity monitor",
}

# NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps
ACTIVATE_OPTIONS = 3

# Seconds to spin the run loop before reading AppKit state -- see frontmost_app().
RUNLOOP_PUMP = 0.02

AX_TIMEOUT = 2.0  # seconds -- a hung app must not wedge the agent's event loop
MAX_NODES = 400  # cap on a describe() walk; a big window's tree is unbounded
MAX_DEPTH = 12
RETURN_KEYCODE = 36

# US-layout virtual keycodes, enough to express the shortcuts an assistant
# actually needs. Anything not here is rejected by name rather than guessed
# at -- a wrong keycode in a granted app is a silent misfire, not an error.
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32,
    "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25, "0": 29,
    "-": 27, "=": 24, "[": 33, "]": 30, "\\": 42, ";": 41, "'": 39, ",": 43, ".": 47, "/": 44,
    "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "forwarddelete": 117,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
}

# CGEventFlags
MODIFIERS = {
    "cmd": 1 << 20, "command": 1 << 20,
    "shift": 1 << 17,
    "ctrl": 1 << 18, "control": 1 << 18,
    "alt": 1 << 19, "option": 1 << 19, "opt": 1 << 19,
    "fn": 1 << 23,
}
TYPE_CHUNK = 20  # CGEventKeyboardSetUnicodeString is unreliable on long strings


class UIAccessError(Exception):
    """Raised for the expected failure modes (no permission, no such app, a
    denied app). Tools catch this and turn it into the usual {"ok": False}."""


def is_denied(app_name: str) -> bool:
    return app_name.strip().lower() in DENIED_APPS


def ensure_trusted() -> None:
    """Accessibility permission is granted to the *responsible* process, not
    the one making the call -- under `npm start` that's Electron, standalone
    it's whatever terminal launched the agent. Untrusted AX calls don't
    error, they return empty trees, so check explicitly and fail loudly."""
    if not AXIsProcessTrusted():
        raise UIAccessError(
            "Accessibility permission is not granted. Enable it for the app that "
            "launched this agent (Electron in dev, or your terminal if you ran it "
            "standalone) in System Settings > Privacy & Security > Accessibility."
        )


def _pump() -> None:
    """Let AppKit process pending notifications before its state is read.

    NSWorkspace's view of the world -- both the running-app list and which app
    is frontmost -- is updated by notifications, which are only delivered on a
    run loop turn. This agent has no AppKit run loop, so without this every
    such read returns the snapshot from process start, forever. It is not a
    timing nicety: an app launched after the agent booted is invisible to it
    permanently. (Reproduced: with Notes launched after startup, an unpumped
    read never listed it, while a pumped read saw it within a second.)
    """
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, RUNLOOP_PUMP, False)


def _running_apps_raw() -> dict:
    apps = {}
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.activationPolicy() != 0:  # NSApplicationActivationPolicyRegular
            continue
        name = app.localizedName()
        if name:
            apps[name] = app.processIdentifier()
    return apps


def running_apps() -> dict:
    """Visible app name -> pid, for regular (non-background) applications."""
    _pump()
    return _running_apps_raw()


def frontmost_app() -> str:
    """Which app currently has keyboard focus, read fresh every call.

    Two non-obvious things, both measured rather than assumed:

    NSWorkspace.frontmostApplication() is updated by a notification, so in a
    process with no AppKit run loop -- exactly what this agent is -- it
    returns whatever it saw the first time and never changes. Measured: four
    activations over 12s, all correctly performed, while it reported the
    original app throughout. Pumping the run loop for a moment first fixes it
    completely (it then caught up within 41-75ms every time).

    The Accessibility system-wide element is the obvious alternative and does
    track switches instantly, but it returns kAXErrorCannotComplete (-25204)
    when the focused app doesn't answer AX messaging -- reproduced with two
    different Chromium/Electron apps, which is precisely the kind of app
    someone would want to dictate into. So it's the cross-check, not the
    primary.
    """
    _pump()
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is not None and app.localizedName():
        return app.localizedName()

    system_wide = AXUIElementCreateSystemWide()
    err, ax_app = AXUIElementCopyAttributeValue(system_wide, "AXFocusedApplication", None)
    if err != 0 or ax_app is None:
        return ""
    err, pid = AXUIElementGetPid(ax_app, None)
    if err == 0 and pid:
        for name, candidate_pid in _running_apps_raw().items():  # just pumped above
            if candidate_pid == pid:
                return name
    return ""


def resolve_app(name: str) -> tuple:
    """Match a spoken/typed app name against what's actually running.

    Returns (canonical_name, pid). Exact case-insensitive match wins over a
    prefix match, which wins over a substring match -- so "notes" doesn't
    resolve to "Notes Widget" when plain "Notes" is running.
    """
    apps = running_apps()
    target = name.strip().lower()

    for candidate, pid in apps.items():
        if candidate.lower() == target:
            return candidate, pid
    for candidate, pid in apps.items():
        if candidate.lower().startswith(target):
            return candidate, pid
    for candidate, pid in apps.items():
        if target in candidate.lower():
            return candidate, pid

    raise UIAccessError(f"no running app matches {name!r}; running: {', '.join(sorted(apps))}")


def activate(name: str, timeout: float = 5.0) -> str:
    """Launch if needed and bring to the front. Returns the canonical name.

    `open -a` alone is not enough: it launches, but macOS's focus-stealing
    prevention means it does not reliably raise an already-running app when
    the caller isn't itself frontmost (measured: the target stayed behind for
    4s+ while `open` reported success). Activation has to be asked for
    explicitly via NSRunningApplication, then waited on -- it's asynchronous,
    and returning early would let a following type_text go to the old app.
    """
    if is_denied(name):
        raise UIAccessError(f"{name} is on the never-drivable list and cannot be focused")

    ensure_trusted()

    try:
        canonical, pid = resolve_app(name)
    except UIAccessError:
        # Not running yet -- launch it, then wait for it to register.
        result = subprocess.run(["open", "-a", name], capture_output=True, text=True)
        if result.returncode != 0:
            raise UIAccessError(result.stderr.strip() or f"could not open {name}")
        canonical, pid = None, None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                canonical, pid = resolve_app(name)
                break
            except UIAccessError:
                time.sleep(0.15)
        if pid is None:
            raise UIAccessError(
                f"launched {name} but it never appeared in the running-app list within "
                f"{timeout}s -- it may be a background-only or non-standard app"
            )

    running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if running is None:
        raise UIAccessError(f"{canonical} is no longer running")
    running.activateWithOptions_(ACTIVATE_OPTIONS)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if frontmost_app() == canonical:
            return canonical
        time.sleep(0.1)

    # It's running but never came forward (a modal elsewhere, a slow cold
    # start, Stage Manager). Report that rather than pretending focus landed,
    # since the caller is about to type somewhere.
    raise UIAccessError(f"{canonical} did not come to the front within {timeout}s")


def _attr(element, name):
    err, value = AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def _node(element, depth: int, path: str, budget: list) -> dict:
    """One AX element as a plain dict, recursing into children.

    `budget` is a single-item list used as a mutable counter shared across the
    whole walk, so MAX_NODES caps the total tree rather than each branch.
    """
    budget[0] -= 1
    role = _attr(element, "AXRole") or "?"
    node = {"path": path, "role": str(role)}

    title = _attr(element, "AXTitle")
    if title:
        node["title"] = str(title)

    # Never surface a password field's contents -- see the module docstring.
    if str(role) == "AXSecureTextField":
        node["value"] = "<redacted>"
    else:
        value = _attr(element, "AXValue")
        if isinstance(value, (str, int, float, bool)):
            text = str(value)
            node["value"] = text if len(text) <= 500 else text[:500] + "…"

    if _attr(element, "AXFocused") is True:
        node["focused"] = True

    if depth >= MAX_DEPTH or budget[0] <= 0:
        return node

    children = _attr(element, "AXChildren") or []
    kids = []
    for index, child in enumerate(children):
        if budget[0] <= 0:
            node["truncated"] = True
            break
        kids.append(_node(child, depth + 1, f"{path}/{index}", budget))
    if kids:
        node["children"] = kids
    return node


def describe(app_name: str) -> dict:
    """Accessibility tree for an app's focused (or first) window."""
    ensure_trusted()
    if is_denied(app_name):
        raise UIAccessError(f"{app_name} is on the never-readable list")

    canonical, pid = resolve_app(app_name)
    app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, AX_TIMEOUT)

    window = _attr(app, "AXFocusedWindow")
    if window is None:
        windows = _attr(app, "AXWindows") or []
        window = windows[0] if windows else None
    if window is None:
        raise UIAccessError(f"{canonical} has no open window to describe")

    budget = [MAX_NODES]
    tree = _node(window, 0, "", budget)
    return {"app": canonical, "tree": tree, "nodes_remaining": budget[0]}


def focused_context() -> dict:
    """Where keystrokes would actually land right now: the frontmost app, its
    focused window's title, and the role of the focused control.

    Worth reporting on every type, because "the app is frontmost" is not the
    same as "this is the document the user meant". Caught in testing: opening
    TextEdit put its *Open* dialog in front, and a perfectly successful type
    went into the file picker's filename field. Returning this alongside the
    result gives the model the same chance to notice that a person would get
    from looking at the screen.
    """
    app_name = frontmost_app()
    context = {"app": app_name, "window": None, "role": None, "dialog": False}
    if not app_name:
        return context
    try:
        _, pid = resolve_app(app_name)
    except UIAccessError:
        return context

    app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, AX_TIMEOUT)

    window = _attr(app, "AXFocusedWindow")
    if window is not None:
        title = _attr(window, "AXTitle")
        context["window"] = str(title) if title else None
        subrole = _attr(window, "AXSubrole")
        context["dialog"] = str(subrole) in ("AXDialog", "AXSystemDialog", "AXSheet")

    element = _attr(app, "AXFocusedUIElement")
    if element is not None:
        role = _attr(element, "AXRole")
        context["role"] = str(role) if role else None

    return context


def _clear_flags(event) -> None:
    """Strip inherited modifier flags from a synthetic event.

    A newly created CGEvent picks up the *current* global modifier state
    rather than starting clean. Measured right after sending cmd+n: a fresh
    event carried 0x20100000 (command | non-coalesced), so every character
    that followed was delivered as a command-shortcut instead of text --
    typing silently produced nothing while firing whatever menu items those
    letters happened to be bound to. Any typing that follows a shortcut has
    to clear this explicitly.
    """
    CGEventSetFlags(event, 0)


def _post_unicode(chunk: str) -> None:
    for down in (True, False):
        event = CGEventCreateKeyboardEvent(None, 0, down)
        _clear_flags(event)
        CGEventKeyboardSetUnicodeString(event, len(chunk), chunk)
        CGEventPost(kCGHIDEventTap, event)


def _post_return() -> None:
    for down in (True, False):
        event = CGEventCreateKeyboardEvent(None, RETURN_KEYCODE, down)
        _clear_flags(event)
        CGEventPost(kCGHIDEventTap, event)


def type_text(text: str, expected_app: str = None) -> dict:
    """Send `text` as keystrokes to whatever is frontmost.

    Re-checks focus before every chunk and stops the moment it moves. The
    premise of a long working session is that the user keeps using their
    machine, so focus changing mid-typing isn't an edge case -- it's Tuesday.
    Without this, checking the app once up front and then typing 200
    characters is a time-of-check/time-of-use hole: approve typing into a
    notes app, click over to Slack, and the rest of the sentence goes out in
    a chat box.

    Returns how much actually landed, so a partial write is reported as one
    rather than being silently rounded up to success.

    Newlines are posted as a real Return keypress rather than as a unicode
    character -- many apps ignore a synthesized "\\n" in a text view but
    handle the keycode, and in a single-field context Return is what commits
    the entry.
    """
    ensure_trusted()
    target = expected_app or frontmost_app()
    if not target:
        raise UIAccessError("nothing is frontmost to type into")

    typed = 0
    for line_index, line in enumerate(text.split("\n")):
        if line_index:
            if frontmost_app() != target:
                return {"app": target, "typed": typed, "complete": False}
            _post_return()
            typed += 1
            time.sleep(0.01)
        for start in range(0, len(line), TYPE_CHUNK):
            if frontmost_app() != target:
                return {"app": target, "typed": typed, "complete": False}
            chunk = line[start : start + TYPE_CHUNK]
            _post_unicode(chunk)
            typed += len(chunk)
            time.sleep(0.01)  # a burst with no gap drops characters in some apps

    return {"app": target, "typed": typed, "complete": True}


def parse_keys(keys: str) -> tuple:
    """"cmd+shift+n" -> (modifier flags, keycode). Raises on anything unknown."""
    parts = [part.strip().lower() for part in keys.replace(" ", "+").split("+") if part.strip()]
    if not parts:
        raise UIAccessError("no keys given")

    flags = 0
    key = None
    for part in parts:
        if part in MODIFIERS:
            flags |= MODIFIERS[part]
        elif key is None:
            key = part
        else:
            raise UIAccessError(f"{keys!r} names more than one non-modifier key")

    if key is None:
        raise UIAccessError(f"{keys!r} is only modifiers, with no key to press")
    if key not in KEYCODES:
        raise UIAccessError(f"unknown key {key!r}; known keys: {', '.join(sorted(KEYCODES))}")
    return flags, KEYCODES[key]


def press_keys(keys: str, expected_app: str = None) -> None:
    """Send one keyboard shortcut to the frontmost app.

    Checks focus immediately beforehand for the same reason type_text does:
    a shortcut delivered to the wrong app is worse than a stray character,
    since it's a command rather than content.
    """
    ensure_trusted()
    flags, keycode = parse_keys(keys)

    target = expected_app or frontmost_app()
    if target and frontmost_app() != target:
        raise UIAccessError(f"focus left {target} before {keys} could be sent")

    for down in (True, False):
        event = CGEventCreateKeyboardEvent(None, keycode, down)
        CGEventSetFlags(event, flags)
        CGEventPost(kCGHIDEventTap, event)
        time.sleep(0.01)
