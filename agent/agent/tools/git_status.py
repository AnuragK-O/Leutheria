"""Read-only summary of a git repository: branch, changed files, recent commits.

"Safe" because nothing here can change the repository or run anything the
repository chooses:

  fixed argv         no shell, and the path is one argv element after -C.
  no index refresh   GIT_OPTIONAL_LOCKS=0 stops `git status` from rewriting
                     .git/index, which it otherwise does opportunistically.
  no repo-run code   a repository's own .git/config can name programs for git
                     to run, and a folder someone else made -- an unzipped
                     download -- is exactly where that config comes from
                     (review H3: a `filter.x.clean` with `* filter=x` in
                     .gitattributes ran on `git status`). What can fire in
                     the argv below, and what is done about it:
                       filter.<n>.clean/process  status re-hashes stat-dirty
                           files through them. The config is listed first
                           (read-only, includes followed) and every filter
                           found is overridden to nothing, required=false.
                       submodules  status recurses and runs each one's own
                           config: --ignore-submodules=all.
                       core.fsmonitor  run by status: =false.
                       gpg.program  run by log when log.showSignature:
                           --no-show-signature.
                       hooks (post-index-change)  only on an index write,
                           which GIT_OPTIONAL_LOCKS=0 prevents; hooksPath is
                           pointed at /dev/null as well.
                       pagers  --no-pager, GIT_PAGER=cat, PAGER=cat.
                       askpass, credential helpers, ssh, upload/receive-pack
                           only on network operations, and none is made;
                           GIT_TERMINAL_PROMPT=0 and the askpass variables
                           cleared regardless.
                       diff.external, textconv  only when a diff is shown;
                           status and log --oneline never show one.
                       alias.*  git never lets an alias replace a built-in
                           command, and only built-ins are run.
                       core.editor  only for commands that edit; none here.
                     scripts/security_tools_check.py runs all of these
                     against a repository that sets every one.
  bounded            a timeout per call, capped file list and commit count.

It is also strictly local: it never contacts the remote (a fetch would
write to .git, and would be a network call). So ahead/behind describe the
last fetch, not the remote as it is now, and the result says so.

Anything that writes through git (commit, pull, push, init) goes through
run_command instead, which asks first.
"""

import os
import re
import subprocess
from pathlib import Path

TIMEOUT = 10  # seconds per git call
MAX_FILES = 200
DEFAULT_COMMITS = 5
MAX_COMMITS = 20

UPSTREAM_NOTE = (
    "ahead and behind are measured against the upstream branch as of the last fetch. "
    "The remote was not contacted, so this cannot tell whether there is anything new to pull."
)

_GIT = [
    "git", "--no-pager",
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "diff.ignoreSubmodules=all",
]
_FILTER_KEYS = ("clean", "smudge", "process")


def _env() -> dict:
    env = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "LC_ALL": "C",
    }
    for name in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF"):
        env.pop(name, None)
    return env


def _neutralising_args(path: Path) -> list:
    """`-c filter.<name>.clean=` (and smudge, process, required=false) for
    every filter the repository's config -- local, global, system and
    anything they include -- defines. An empty command means no filter, so
    files are compared as they are on disk. Listing config runs nothing."""
    listed = subprocess.run(
        [*_GIT, "-C", str(path), "config", "--list", "--name-only", "--includes", "-z"],
        capture_output=True, text=True, errors="replace", timeout=TIMEOUT, env=_env(), stdin=subprocess.DEVNULL,
    )
    names = set()
    for key in listed.stdout.split("\0"):
        key = key.strip()
        # filter.<name>.<var>; the name may itself contain dots.
        if key.lower().startswith("filter.") and key.count(".") >= 2:
            names.add(key[len("filter."): key.rindex(".")])
    args = []
    for name in sorted(names):
        args += [arg for var in _FILTER_KEYS for arg in ("-c", f"filter.{name}.{var}=")]
        args += ["-c", f"filter.{name}.required=false"]
    return args


def _git(path: Path, *args: str, overrides: list = ()) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*_GIT, *overrides, "-C", str(path), *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=TIMEOUT,
        env=_env(),
        stdin=subprocess.DEVNULL,
    )


def _strip_credentials(url: str) -> str:
    """https://user:token@host/x -> https://host/x. A remote URL can carry a
    personal access token, and this result goes to the model and the log."""
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^/@]+@", r"\1", url.strip(), flags=re.I)


def _parse_branch(line: str) -> dict:
    info = {"branch": None, "upstream": None, "ahead": 0, "behind": 0, "detached": False, "no_commits": False}
    head = line[3:] if line.startswith("## ") else line
    if head.startswith("No commits yet on "):
        info["no_commits"] = True
        head = head[len("No commits yet on "):]
    elif head.startswith("Initial commit on "):
        info["no_commits"] = True
        head = head[len("Initial commit on "):]
    if head.startswith("HEAD (no branch)"):
        info["detached"] = True
        return info
    tracking = re.search(r" \[(.+)\]$", head)
    if tracking:
        head = head[: tracking.start()]
        for part in tracking.group(1).split(", "):
            kind, _, count = part.partition(" ")
            if kind in ("ahead", "behind") and count.isdigit():
                info[kind] = int(count)
    branch, sep, upstream = head.partition("...")
    info["branch"] = branch.strip() or None
    info["upstream"] = upstream.strip() or None if sep else None
    return info


def _summary(info: dict, counts: dict, commits: list) -> str:
    if info["detached"]:
        where = "Detached HEAD"
    else:
        where = f"On branch {info['branch']}"
    if info["no_commits"]:
        where += " with no commits yet"
    changes = [f"{n} {label}" for label, n in counts.items() if n]
    parts = [where, ", ".join(changes) if changes else "working tree clean"]
    if info["upstream"]:
        # "0 behind" would read as "nothing to pull", which this can't know.
        if info["ahead"]:
            parts.append(f"{info['ahead']} ahead of {info['upstream']}")
        if info["behind"]:
            parts.append(f"{info['behind']} behind {info['upstream']}")
        parts.append("compared with the last fetch, the remote was not checked")
    if commits:
        parts.append(f"last commit: {commits[0]['subject']}")
    return "; ".join(parts) + "."


def git_status(path: str, log_count: int = DEFAULT_COMMITS) -> dict:
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "no folder given"}
    target = Path(path.strip()).expanduser()
    if not target.is_dir():
        return {"ok": False, "error": f"{target} is not a folder"}
    if isinstance(log_count, bool) or not isinstance(log_count, int):
        log_count = DEFAULT_COMMITS
    log_count = max(0, min(log_count, MAX_COMMITS))

    try:
        # --untracked-files=all: list the files inside a new folder rather
        # than just the folder, so the counts are files and a caller can see
        # exactly what a commit would pick up.
        overrides = _neutralising_args(target)
        status = _git(
            target, "status", "--porcelain=v1", "-b", "--untracked-files=all", "--ignore-submodules=all",
            overrides=overrides,
        )
        if status.returncode != 0:
            err = status.stderr.strip()
            if "not a git repository" in err.lower():
                return {"ok": False, "error": f"{target} is not a git repository"}
            if "dubious ownership" in err.lower():
                return {"ok": False, "error": f"git refuses to read {target} because another user owns it"}
            return {"ok": False, "error": (err.splitlines() or ["git status failed"])[-1][:300]}

        commits = []
        if log_count:
            # --no-show-signature: log.showSignature in the repo's config
            # would otherwise run whatever gpg.program names.
            log = _git(
                target, "log", "-n", str(log_count), "--oneline", "--no-decorate", "--no-show-signature",
                overrides=overrides,
            )
            if log.returncode == 0:  # fails on a repo with no commits: that's just "none"
                for line in log.stdout.splitlines():
                    sha, _, subject = line.partition(" ")
                    commits.append({"hash": sha, "subject": subject[:200]})

        top = _git(target, "rev-parse", "--show-toplevel", overrides=overrides)
        remote = _git(target, "remote", "get-url", "origin", overrides=overrides)
    except FileNotFoundError:
        return {"ok": False, "error": "git is not installed"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"git did not answer within {TIMEOUT} seconds"}

    lines = status.stdout.splitlines()
    info = _parse_branch(lines[0]) if lines and lines[0].startswith("##") else _parse_branch("## ")
    counts = {"staged": 0, "modified": 0, "untracked": 0, "conflicted": 0}
    files = []
    for line in lines:
        if line.startswith("##") or len(line) < 4:
            continue
        x, y, name = line[0], line[1], line[3:]
        if x == "?" and y == "?":
            counts["untracked"] += 1
        elif x == "U" or y == "U" or (x == y and x in "AD"):
            counts["conflicted"] += 1
        else:
            if x != " ":
                counts["staged"] += 1
            if y != " ":
                counts["modified"] += 1
        if len(files) < MAX_FILES:
            files.append({"status": (x + y).strip() or "?", "path": name[:300]})

    changed = sum(1 for line in lines if not line.startswith("##") and len(line) >= 4)
    return {
        "ok": True,
        "path": str(target),
        "root": top.stdout.strip() if top.returncode == 0 else str(target),
        **info,
        "clean": changed == 0,
        "changed_files": changed,
        **counts,
        "files": files,
        "files_truncated": changed > len(files),
        "commits": commits,
        "remote_url": _strip_credentials(remote.stdout) if remote.returncode == 0 else None,
        # ahead/behind come from the local copy of the upstream branch. Said
        # in the result because a bare "behind: 0" was read as "up to date".
        "remote_checked": False,
        "upstream_note": UPSTREAM_NOTE if info["upstream"] else None,
        "message": _summary(info, counts, commits),
    }
