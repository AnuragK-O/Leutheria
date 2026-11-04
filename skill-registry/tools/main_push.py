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
    """The remote without credentials (the workflows' URLs carry a token)."""
    parts = urllib.parse.urlsplit(remote)
    if parts.username or parts.password:
        host = parts.hostname + (f":{parts.port}" if parts.port else "")
        return urllib.parse.urlunsplit(parts._replace(netloc=host))
    return remote


def sync(remote: str, work: Path) -> None:
    if not (work / ".git").is_dir():
        if work.exists() and any(work.iterdir()):
            raise Permanent(f"{work} exists and is not this tool's clone; pass an empty or missing --work")
        work.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "--quiet", "--no-tags", "--single-branch", "--branch", "main",
                            remote, str(work)], capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if r.returncode != 0:
            raise GitFailed(f"git clone failed: {r.stderr.strip()[-300:]}")
        return
    git(work, "fetch", "--quiet", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    git(work, "checkout", "--quiet", "--force", "-B", "main", "refs/remotes/origin/main")
    git(work, "reset", "--quiet", "--hard", "refs/remotes/origin/main")
    git(work, "clean", "-qfdx")


def discard(remote: str, work: Path) -> None:
    """Delete <work> so the next attempt clones afresh -- only if it is a clone of
    <remote> (its origin), never some other directory a caller passed by mistake."""
    if not (work / ".git").is_dir():
        return  # nothing cloned (a failed clone cleans up after itself); next attempt clones
    r = subprocess.run(["git", "config", "--file", str(work / ".git" / "config"), "remote.origin.url"],
                       capture_output=True, text=True, timeout=GIT_TIMEOUT)
    url = r.stdout.strip()
    same = url == remote or (url and Path(url).expanduser().resolve() == Path(remote).expanduser().resolve())
    if r.returncode != 0 or not same:
        raise Permanent(f"{work} looks broken but is not this tool's clone of the remote; not deleting it")
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
            discard(remote, work)
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
