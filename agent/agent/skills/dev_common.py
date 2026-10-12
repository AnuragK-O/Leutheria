"""Shared plumbing for the dev / web / clipboard skills.

Not a skill itself. Everything a skill *does* still goes through dispatch()
(Steps.run below is the only caller), so preferences, confirmation and the
safety tiers apply to every step exactly as if the model had called it.
What lives here besides that is argument checking and read-only
preconditions (does the folder exist, is this a URL).
"""

import re
import shlex
import urllib.parse
from datetime import datetime
from pathlib import Path

DEFAULT_PROJECTS_DIR = "~/Projects"
DEFAULT_NOTES_DIR = "~/Notes"
DEFAULT_EDITOR = "Visual Studio Code"

# A folder name, not a path: no separators, no leading dot or dash, nothing
# that needs escaping when it is written into a generated source file.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")


class Steps:
    """Runs a skill's steps through dispatch() and keeps the trace."""

    def __init__(self, websocket, pending):
        self.websocket = websocket
        self.pending = pending
        self.trace = []

    async def run(self, tool: str, args: dict) -> dict:
        # Imported lazily: dispatcher imports the skills registry, which
        # imports the skill modules, which import this.
        from agent.core.dispatcher import dispatch

        result = await dispatch(tool, args, self.websocket, self.pending)
        if not isinstance(result, dict):
            result = {"ok": False, "error": f"{tool} returned no result"}
        self.trace.append({"tool": tool, "args": args, "result": result})
        return result

    def fail(self, error: str, **extra) -> dict:
        return {"ok": False, "error": error, "message": error, "steps": self.trace, **extra}

    def step_failed(self, what: str, result: dict, **extra) -> dict:
        """`what` names the step in words a listener follows ("creating the
        folder"), and the tool's own error says why."""
        reason = result.get("error") or result.get("stderr") or "no reason given"
        return self.fail(f"Stopped at {what}: {reason}", **extra)

    def done(self, message: str, **extra) -> dict:
        return {"ok": True, "message": message, "steps": self.trace, **extra}


def text_arg(args: dict, key: str, default: str = "") -> str:
    """A string argument, stripped; anything that isn't a string is the default."""
    value = args.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else default


def flag(args: dict, key: str) -> bool:
    return args.get(key) is True


def expand(path: str) -> Path:
    return Path(path).expanduser()


def q(value) -> str:
    """Quote one value for a shell string. Every piece of user or model text
    that goes into a run_command string goes through this."""
    return shlex.quote(str(value))


def stamp(fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.now().strftime(fmt)


def slug(text: str, fallback: str = "untitled", limit: int = 50) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return cleaned or fallback


def project_root(args: dict) -> tuple:
    """(root Path, name, error). The root is base_dir/name with ~ expanded."""
    name = text_arg(args, "name")
    if not _NAME_RE.match(name) or name.endswith((".", " ")):
        return None, name, (
            "A project name must be a plain folder name: letters, digits, spaces, "
            "dots, dashes or underscores, starting with a letter or digit."
        )
    return expand(text_arg(args, "base_dir", DEFAULT_PROJECTS_DIR)) / name, name, None


async def scaffold(steps: Steps, root: Path, files: dict):
    """Create root and write each file in it. Returns a failure result, or
    None when everything was written.

    Never touches an existing project: an existing non-empty folder (or a
    file of that name) stops it before anything is created, and every file
    goes through write_new_file, which refuses to replace one.
    """
    if root.exists():
        if not root.is_dir():
            return steps.fail(f"{root} already exists and is not a folder, so nothing was created.")
        if any(root.iterdir()):
            return steps.fail(f"{root} already exists and is not empty, so nothing was created.")

    result = await steps.run("create_folder", {"path": str(root)})
    if not result.get("ok"):
        return steps.step_failed("creating the folder", result)

    for relative, content in files.items():
        target = root / relative
        if target.parent != root:
            result = await steps.run("create_folder", {"path": str(target.parent)})
            if not result.get("ok"):
                return steps.step_failed(f"creating {target.parent.name}", result)
        result = await steps.run("write_new_file", {"path": str(target), "content": content})
        if not result.get("ok"):
            return steps.step_failed(f"writing {relative}", result, created=str(root))
    return None


async def optional_command(steps: Steps, what: str, cmd: str, root: Path):
    """An opt-in step that goes through run_command, so the user is asked.
    Returns a failure result (naming what already exists) or None."""
    result = await steps.run("run_command", {"cmd": cmd})
    if not result.get("ok"):
        return steps.step_failed(
            what, result, created=str(root),
        ) | {"message": f"The project was created at {root}, but {what} failed or was declined."}
    return None


async def open_in_app(steps: Steps, path: Path, app: str, root: Path = None):
    result = await steps.run("open_with", {"path": str(path), "app": app})
    if not result.get("ok"):
        failure = steps.step_failed(f"opening it in {app}", result)
        if root is not None:
            failure["created"] = str(root)
            failure["message"] = f"Created {root}, but could not open it in {app}: {result.get('error')}"
        return failure
    return None


def http_url(text: str):
    """The text as an http(s) URL, or None. A bare 'example.com/page' gets
    https:// put in front; anything with another scheme is not a web page."""
    candidate = (text or "").strip()
    if not candidate or len(candidate) > 2000 or any(c.isspace() for c in candidate):
        return None
    if "://" not in candidate:
        if candidate.lower().startswith(("mailto:", "javascript:", "data:", "file:", "tel:")):
            return None
        candidate = "https://" + candidate
    try:
        parts = urllib.parse.urlsplit(candidate)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    if "." not in host and host != "localhost":
        return None
    return candidate


async def url_or_clipboard(steps: Steps, args: dict, key: str = "url") -> tuple:
    """(url, from_clipboard, failure). With no URL argument, the clipboard is
    used if -- and only if -- what's on it is a single web address."""
    given = text_arg(args, key)
    if given:
        url = http_url(given)
        if url is None:
            return None, False, steps.fail(f"That is not a web address I can use: {given[:120]}")
        return url, False, None

    result = await steps.run("get_clipboard", {})
    if not result.get("ok"):
        return None, True, steps.step_failed("reading the clipboard", result)
    url = http_url(result.get("text") or "")
    if url is None:
        return None, True, steps.fail(
            "No web address was given and the clipboard does not hold one. "
            "Copy the page's address first, or tell me the address."
        )
    return url, True, None
