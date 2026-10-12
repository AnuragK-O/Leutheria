import re
import urllib.parse

from agent.skills import dev_common as dc

_SHORT_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def clone_url(repo: str) -> tuple:
    """(url, default folder name) for 'owner/name' or an https URL, else
    (None, None). https only: no ssh, no local paths, none of git's other
    transports (ext:: runs a command)."""
    repo = repo.strip()
    if _SHORT_RE.match(repo):
        name = repo.split("/")[1]
        return f"https://github.com/{repo}", name[:-4] if name.endswith(".git") else name
    if any(c.isspace() for c in repo) or len(repo) > 500:
        return None, None
    try:
        parts = urllib.parse.urlsplit(repo)
        host = parts.hostname
    except ValueError:
        return None, None
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password:
        return None, None
    name = parts.path.rstrip("/").split("/")[-1]
    name = name[:-4] if name.endswith(".git") else name
    return (repo, name) if _NAME_RE.match(name) else (None, None)


async def run(args: dict, websocket, pending: dict) -> dict:
    """Clone a repository into a new folder. The destination must not exist,
    so nothing is ever cloned over; the clone is a run_command and asks."""
    steps = dc.Steps(websocket, pending)
    url, default_name = clone_url(dc.text_arg(args, "repo"))
    if url is None:
        return steps.fail("Give the repository as owner slash name on GitHub, or as an https address.")
    name = dc.text_arg(args, "name", default_name)
    if not _NAME_RE.match(name):
        return steps.fail("The folder name must be letters, digits, dots, dashes or underscores.")
    base = dc.expand(dc.text_arg(args, "base_dir", dc.DEFAULT_PROJECTS_DIR))
    dest = base / name
    if dest.exists():
        return steps.fail(f"{dest} already exists, so nothing was cloned.")

    result = await steps.run("create_folder", {"path": str(base)})
    if not result.get("ok"):
        return steps.step_failed("creating the parent folder", result)
    result = await steps.run("run_command", {"cmd": f"git clone -- {dc.q(url)} {dc.q(dest)}"})
    if not result.get("ok"):
        return steps.step_failed("git clone", result)

    app = dc.text_arg(args, "open_in")
    if app:
        failure = await dc.open_in_app(steps, dest, app, dest)
        if failure:
            return failure
    return steps.done(f"Cloned {url} into {dest}" + (f" and opened it in {app}." if app else "."), path=str(dest))
