"""Change main by fast-forward only, recomputing the change from a fresh main on every try.

Shared by record_push.py (review records) and yank_push.py (yanks): the CI steps that
write to main while other runs may be writing to it too.

    push_to_main(remote, work, apply, message, ...)

Each attempt syncs <work> (this helper's own clone of <remote>: created if missing,
hard-reset and cleaned every time, so never a checkout anyone cares about) to main
exactly, then calls apply(work). apply makes the change in the tree and returns a
result, returns ALREADY (the change is on main already: success, nothing to push), or
raises Permanent (retrying can't help). The change is committed and pushed as a plain
fast-forward (never forced), so it lands only on the exact main apply() read; when
another write lands first the push is rejected and the next attempt starts over from
the new main, after a jittered exponential backoff so a burst spreads out instead of
retrying in lockstep. A clone that is broken rather than behind (git reports missing or
damaged objects, or RECLONE_AFTER failures in a row that aren't a rejected push) is
deleted and cloned afresh, since fetch never re-sends objects it thinks it has.
Returns ("pushed", result) or ("already", None); raises Permanent, or GaveUp after the
attempts or the deadline.
"""

import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

GIT_TIMEOUT = 120
BOT_NAME = "leutheria-registry[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
ALREADY = object()
# What git says when the clone itself is broken (objects missing or damaged) rather than
# behind: fetch never re-sends objects it believes the clone has, so retrying in place
# fails the same way forever. Seen for real: a clone that copied loose objects while gc
# repacked them, then "invalid object <blob> for reviews/..." on all 400 attempts.
CORRUPT_RE = re.compile(r"invalid object|bad object|missing (blob|tree|commit)|unable to read|corrupt|"
                        r"loose object|packfile|did not send all necessary objects|fsck|"
                        r"not a tree|could not parse|is not a valid", re.IGNORECASE)
# Failures in a row, other than a rejected push, after which the clone is rebuilt anyway.
RECLONE_AFTER = 3


class Permanent(Exception):
    """Retrying can't change the outcome."""


class GaveUp(Exception):
    def __init__(self, tries, elapsed, last):
        super().__init__(f"gave up after {tries} attempts ({elapsed:.0f}s); last error: {last}")
        self.tries, self.elapsed, self.last = tries, elapsed, last


class GitFailed(Exception):
    pass


def git(work: Path, *args, check=True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(work), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if check and r.returncode != 0:
        raise GitFailed(f"git {args[0]} failed: {r.stderr.strip()[-300:]}")
    return r


def display_remote(remote: str) -> str:
    """The remote without credentials (the workflows' URLs carry a token). A URL
    urllib can't parse (a bad port, an unclosed "[") loses everything before "@"."""
    try:
        parts = urllib.parse.urlsplit(remote)
        if parts.username or parts.password:
            host = parts.hostname + (f":{parts.port}" if parts.port else "")
            return urllib.parse.urlunsplit(parts._replace(netloc=host))
    except ValueError:
        return re.sub(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/]*@", r"\1", remote)
    return remote


def _unparseable(url: str) -> str | None:
    """Why urllib can't parse <url>, in words that never quote it (a token may sit
    where a port should be), or None if it parses."""
    try:
        remote_identity(url)
    except ValueError as e:
        msg = str(e).lower()
        return ("a port that isn't a number from 0 to 65535" if "port" in msg else
                "an unclosed IPv6 bracket" if "ipv6" in msg else "a malformed host part")
    return None


_SCP_RE = re.compile(r"^(?:[^@/:]+@)?([^/:]+):(?!//)(.*)$")  # git@github.com:o/r.git


def remote_identity(remote: str) -> tuple:
    """What makes two remote URLs the same repository: (scheme, host, port, path),
    with any credentials dropped (the workflows' URLs carry a token, which expires and
    differs between runs) and a trailing "/" or ".git" ignored. A local path, or a
    file: URL, compares by its resolved path."""
    def path_key(path: str) -> str:
        path = path.rstrip("/")
        return path[:-4] if path.endswith(".git") else path

    parts = urllib.parse.urlsplit(remote)
    if parts.scheme == "file":
        return ("file", "", None, str(Path(urllib.parse.unquote(parts.path)).expanduser().resolve()))
    if len(parts.scheme) > 1 and parts.netloc:  # len > 1: "C:" is a drive, not a scheme
        return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port, path_key(parts.path))
    m = _SCP_RE.match(remote)
    if m and not remote.startswith(("/", ".", "~")):
        return ("ssh", m.group(1).lower(), None, path_key("/" + m.group(2).lstrip("/")))
    return ("file", "", None, str(Path(remote).expanduser().resolve()))


def remote_difference(stored: str, remote: str) -> str | None:
    """None when <stored> and <remote> name the same repository (credentials aside);
    else which parts differ, built from remote_identity, so never a token."""
    a, b = remote_identity(stored), remote_identity(remote)
    if a == b:
        return None
    show = lambda v: "(none)" if v in ("", None) else str(v)  # noqa: E731
    return "; ".join(f"{name} {show(x)} vs {show(y)}"
                     for name, x, y in zip(("scheme", "host", "port", "path"), a, b) if x != y)


def origin_url(work: Path) -> str | None:
    r = subprocess.run(["git", "config", "--file", str(work / ".git" / "config"), "remote.origin.url"],
                       capture_output=True, text=True, timeout=GIT_TIMEOUT)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def check_origin(remote: str, work: Path, consequence: str) -> str:
    """<work>'s stored origin URL if it is a clone of <remote> (credentials aside);
    otherwise Permanent, saying what differed -- with credentials stripped."""
    url = origin_url(work)
    if url is None:
        raise Permanent(f"{work} has no origin remote, so it is not this tool's clone of "
                        f"{display_remote(remote)}; {consequence}")
    # A URL urllib can't parse raises ValueError from remote_identity; that is a
    # wrong --work or --remote, not a reason to crash. Neither URL is quoted then.
    for which, value in (("the origin URL stored in it", url), ("the --remote URL", remote)):
        why = _unparseable(value)
        if why is not None:
            raise Permanent(f"{work}: {which} can't be parsed ({why}), so it can't be shown to be this "
                            f"tool's clone of the remote; {consequence}")
    diff = remote_difference(url, remote)
    if diff is not None:
        raise Permanent(f"{work} is a clone of {display_remote(url)}, not of {display_remote(remote)} "
                        f"({diff}; credentials are not compared); {consequence}")
    return url


def sync(remote: str, work: Path) -> None:
    # Before a fresh clone too: git would refuse the URL the same way on every attempt,
    # so retrying it until the deadline only delays the caller's recovery path.
    why = _unparseable(remote)
    if why is not None:
        raise Permanent(f"the --remote URL can't be parsed ({why}); pass a valid --remote")
    if not (work / ".git").is_dir():
        if work.exists() and any(work.iterdir()):
            raise Permanent(f"{work} exists and is not this tool's clone; pass an empty or missing --work")
        work.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "--quiet", "--no-tags", "--single-branch", "--branch", "main",
                            remote, str(work)], capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if r.returncode != 0:
            raise GitFailed(f"git clone failed: {r.stderr.strip()[-300:]}")
        return
    # A reused clone (a manual recovery run, say, with a fresh token): the same repository
    # is fine, and origin takes the URL given now, so a stored expired token is not what
    # fetch and push use. Some other repository is refused before anything is fetched.
    url = check_origin(remote, work, "pass an empty or missing --work")
    if url != remote:
        git(work, "remote", "set-url", "origin", remote)
    git(work, "fetch", "--quiet", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    git(work, "checkout", "--quiet", "--force", "-B", "main", "refs/remotes/origin/main")
    git(work, "reset", "--quiet", "--hard", "refs/remotes/origin/main")
    git(work, "clean", "-qfdx")


def discard(remote: str, work: Path) -> None:
    """Delete <work> so the next attempt clones afresh -- only if it is a clone of
    <remote> (its origin, credentials aside), never some other directory a caller
    passed by mistake. May raise OSError / TimeoutExpired (push_to_main handles them)."""
    if not (work / ".git").is_dir():
        return  # nothing cloned (a failed clone cleans up after itself); next attempt clones
    check_origin(remote, work, "it looks broken, but it is not deleted")
    shutil.rmtree(work)


def push_to_main(remote: str, work: Path, apply, message: str, *, attempts=60, deadline=1800.0,
                 base_delay=2.0, max_delay=60.0, git_name=BOT_NAME, git_email=BOT_EMAIL):
    started = time.monotonic()
    last = "no attempt made"
    tries = 0
    odd_failures = 0  # in a row; a rejected push (the race) resets it
    while tries < attempts:
        tries += 1
        broken = False
        try:
            sync(remote, work)
            result = apply(work)
            if result is ALREADY:
                return "already", None
            git(work, "add", "-A")
            git(work, "-c", f"user.name={git_name}", "-c", f"user.email={git_email}",
                "commit", "--quiet", "-m", message)
            push = git(work, "push", "--quiet", "origin", "HEAD:refs/heads/main", check=False)
            if push.returncode == 0:
                return "pushed", result
            last = push.stderr.strip()[-300:] or "push rejected"
            broken = bool(CORRUPT_RE.search(last))
            odd_failures = odd_failures + 1 if broken else 0
        except (GitFailed, subprocess.TimeoutExpired, OSError) as e:
            last = str(e)
            broken = bool(CORRUPT_RE.search(last))
            odd_failures += 1
        if broken or odd_failures >= RECLONE_AFTER:
            # The work clone is this helper's own: throw it away and clone afresh next time.
            print(f"attempt {tries}: work clone looks broken ({last[:120]}); re-cloning", file=sys.stderr)
            try:
                discard(remote, work)
            except (subprocess.TimeoutExpired, OSError) as e:
                # The clone can't be cleared (permissions, a hung git, a busy disk), and
                # retrying inside it fails the same way: stop with the caller's recovery
                # path rather than a traceback.
                raise GaveUp(tries, time.monotonic() - started,
                             f"{last}; then the broken work clone {work} could not be deleted: "
                             f"{type(e).__name__}: {e}") from e
            odd_failures = 0
        elapsed = time.monotonic() - started
        delay = random.uniform(base_delay / 2, min(max_delay, base_delay * 2 ** (tries - 1)))
        if tries >= attempts or elapsed + delay > deadline:
            break
        print(f"attempt {tries}: main moved or push failed; retrying in {delay:.1f}s", file=sys.stderr)
        time.sleep(delay)
    raise GaveUp(tries, time.monotonic() - started, last)


def add_retry_args(parser) -> None:
    parser.add_argument("--remote", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=60)
    parser.add_argument("--deadline", type=float, default=1800, help="seconds; no new attempt starts after this")
    parser.add_argument("--base-delay", type=float, default=2.0)
    parser.add_argument("--max-delay", type=float, default=60.0)
    parser.add_argument("--git-name", default=BOT_NAME)
    parser.add_argument("--git-email", default=BOT_EMAIL)


def retry_kwargs(args) -> dict:
    return {"attempts": args.attempts, "deadline": args.deadline, "base_delay": args.base_delay,
            "max_delay": args.max_delay, "git_name": args.git_name, "git_email": args.git_email}
