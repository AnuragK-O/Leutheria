import re
from urllib.parse import quote_plus

from agent.skills import dev_common as dc

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_REPO_RE = re.compile(rf"^{_OWNER}/[A-Za-z0-9._-]{{1,100}}$")
_OWNER_RE = re.compile(rf"^{_OWNER}$")
# https://github.com/o/r(.git), git@github.com:o/r(.git), ssh://git@github.com/o/r(.git)
_REMOTE_RE = re.compile(
    rf"^(?:https?://|ssh://git@|git@)(?:www\.)?github\.com[:/]({_OWNER}/[A-Za-z0-9._-]+?)(?:\.git)?/?$", re.I
)

SECTIONS = {
    "code": "",
    "issues": "/issues",
    "pulls": "/pulls",
    "actions": "/actions",
    "releases": "/releases",
    "commits": "/commits",
    "branches": "/branches",
    "wiki": "/wiki",
}


def repo_from_remote(remote_url: str):
    match = _REMOTE_RE.match((remote_url or "").strip())
    return match.group(1) if match else None


async def run(args: dict, websocket, pending: dict) -> dict:
    """Open a GitHub page in the browser: a repository (named, or the one a
    local folder was cloned from), one of its sections, a user, or a search."""
    steps = dc.Steps(websocket, pending)
    repo = dc.text_arg(args, "repo")
    path = dc.text_arg(args, "path")
    query = dc.text_arg(args, "query")[:200]
    section = dc.text_arg(args, "section", "code").lower()
    if section in ("pull requests", "prs", "pull_requests"):
        section = "pulls"
    if section not in SECTIONS:
        return steps.fail(f"I can open these parts of a repository: {', '.join(SECTIONS)}.")

    if repo:
        repo = repo_from_remote(repo) or repo.strip().strip("/")
    elif path:
        status = await steps.run("git_status", {"path": path, "log_count": 0})
        if not status.get("ok"):
            return steps.step_failed("reading the repository", status)
        repo = repo_from_remote(status.get("remote_url") or "")
        if not repo:
            return steps.fail("That repository's origin remote is not on GitHub, so there is no page to open.")

    if repo:
        if _REPO_RE.match(repo):
            url = f"https://github.com/{repo}"
            if query:
                url += "/search?q=" + quote_plus(query)
                what = f"a search for {query} in {repo}"
            else:
                url += SECTIONS[section]
                what = repo if section == "code" else f"{section} for {repo}"
        elif _OWNER_RE.match(repo) and not query:
            url, what = f"https://github.com/{repo}", f"the GitHub profile {repo}"
        else:
            return steps.fail("Give the repository as owner slash name, for example torvalds slash linux.")
    elif query:
        url = "https://github.com/search?type=repositories&q=" + quote_plus(query)
        what = f"a GitHub search for {query}"
    else:
        url, what = "https://github.com", "GitHub"

    result = await steps.run("open_url", {"url": url})
    if not result.get("ok"):
        return steps.step_failed("opening the browser", result)
    return steps.done(f"Opened {what}.", url=url)
