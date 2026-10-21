"""Write a merged submission's review record onto main and push it, retrying until it lands.

    record_push.py --remote <url> --work <dir> --path skills/<name>/<hash>.json
                   --verdict auto_approved|human_approved --author <login>
                   [--pr <n>] [--screen <result.json>] [--report <file.md>]
                   [--attempts 60] [--deadline 1800] [--base-delay 2] [--max-delay 60]

submission.yml runs this right after `gh pr merge`; a maintainer runs the same
command to recover a submission that was merged but never recorded (README,
"Recovering"). <work> is this tool's own clone of <remote>: created if
missing, and hard-reset to the remote's main on every attempt, so never point
it at a checkout you care about.

Each attempt: sync <work> to main exactly (fetch, reset --hard, clean), run
record_review.record() on that tree (name ownership re-checked, version =
highest recorded + 1, both against main as it is now), commit, and push
HEAD:main as a plain fast-forward (never forced). The push is accepted only if
main hasn't moved since the fetch, i.e. only if the version and the owner
check were computed from exactly the commit the record lands on. When another
merge's record lands first the push is rejected and the next attempt starts
again from the new main, after a jittered exponential backoff (so a burst of
merges spreads out instead of retrying in lockstep).

Re-running is safe: if main already has this file's record by this author,
that is success ("already recorded"), not an error.

Exit 0  recorded, or already recorded.
Exit 2  refused for good, so it is not retried: the name has a record by
        another author on main, the skill file isn't on main, or main has an
        unreadable or foreign record for this file. The file stays on main
        unindexed (pending) until a maintainer acts.
Exit 1  gave up (attempts or deadline used up, or git kept failing). The
        submission is merged but STRANDED: never indexed until recorded. The
        exact recovery command is printed and written to --report.
Usage errors exit 2 via argparse.
"""

import argparse
import random
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import common
import record_review

GIT_TIMEOUT = 120
BOT_NAME = "leutheria-registry[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class Permanent(Exception):
    """Retrying can't change the outcome."""


class GitFailed(Exception):
    pass


def git(work: Path, *args, check=True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(work), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if check and r.returncode != 0:
        raise GitFailed(f"git {args[0]} failed: {r.stderr.strip()[-300:]}")
    return r


def display_remote(remote: str) -> str:
    """The remote without credentials (the workflow's URL carries a token)."""
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


def already_recorded(work: Path, rel: str, author: str):
    """The record for this exact file by this author: a previous attempt (or
    run) whose push landed even though it looked failed. The file name is the
    content_hash and a published file can't be resubmitted, so a record for it
    by the same author is this submission's."""
    name, digest = common.split_skill_path(rel)
    target = work / "reviews" / name / f"{digest}.json"
    if not target.exists():
        return None
    try:
        rec = common.read_json(target)
        owner = rec["author"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Permanent(f"{target.relative_to(work)} exists but is unreadable")
    if not isinstance(owner, str) or owner.lower() != author.lower():
        raise Permanent(f"{target.relative_to(work)} already exists, recorded for someone else")
    return rec


def attempt(remote, work, args, screen) -> tuple:
    """One try. Returns ("recorded" | "already", record) or ("lost", None)."""
    sync(remote, work)
    rec = already_recorded(work, args.path, args.author)
    if rec is not None:
        return "already", rec
    try:
        rec = record_review.record(work, args.path, args.verdict, screen, args.author, args.pr,
                                   args.published_at or record_review.now_iso())
    except (common.RegistryError, OSError, ValueError) as e:
        raise Permanent(str(e))
    git(work, "add", "reviews")
    pr = f" (PR #{args.pr})" if args.pr is not None else ""
    git(work, "-c", f"user.name={args.git_name}", "-c", f"user.email={args.git_email}",
        "commit", "--quiet", "-m", f"Review record for {args.path}{pr}")
    push = git(work, "push", "--quiet", "origin", "HEAD:refs/heads/main", check=False)
    if push.returncode == 0:
        return "recorded", rec
    return "lost", push.stderr.strip()[-300:]


def recovery_command(args, remote_shown: str) -> str:
    parts = ["python tools/record_push.py", f"--remote {remote_shown}", "--work /tmp/record-review",
             f"--path {args.path}", f"--verdict {args.verdict}", f"--author {args.author}"]
    if args.pr is not None:
        parts.append(f"--pr {args.pr}")
    parts.append("--screen screen.json  # from the run's artifact, if it has one")
    return " \\\n    ".join(parts)


def write_report(path, text: str) -> None:
    if path:
        Path(path).write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a merged submission's review on main, retrying the push")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--verdict", required=True, choices=common.REVIEW_VERDICTS)
    parser.add_argument("--author", required=True)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--screen", type=Path)
    parser.add_argument("--published-at", default=None)
    parser.add_argument("--report", type=Path, help="write a markdown explanation here on exit 1 or 2")
    parser.add_argument("--attempts", type=int, default=60)
    parser.add_argument("--deadline", type=float, default=1800, help="seconds; no new attempt starts after this")
    parser.add_argument("--base-delay", type=float, default=2.0)
    parser.add_argument("--max-delay", type=float, default=60.0)
    parser.add_argument("--git-name", default=BOT_NAME)
    parser.add_argument("--git-email", default=BOT_EMAIL)
    args = parser.parse_args()
    try:
        common.split_skill_path(args.path)
    except common.RegistryError as e:
        parser.error(str(e))
    screen = None
    if args.screen and args.screen.exists():
        try:
            screen = common.read_json(args.screen)
        except ValueError:
            screen = None
    remote_shown = display_remote(args.remote)
    started = time.monotonic()
    last = "no attempt made"
    tries = 0
    while tries < args.attempts:
        tries += 1
        try:
            outcome, detail = attempt(args.remote, args.work, args, screen)
        except Permanent as e:
            print(f"REFUSED  {args.path}: {e}", file=sys.stderr)
            write_report(args.report, (
                f"This submission was merged, but its review record was **not written**: {e}.\n\n"
                f"The skill file stays on main without a record, so it is **not published** (the index "
                f"lists only recorded files). Retrying won't change this. A maintainer should decide "
                f"whether to remove `{args.path}` from main or leave it unpublished.\n"))
            return 2
        except (GitFailed, subprocess.TimeoutExpired, OSError) as e:
            outcome, detail = "lost", str(e)
        if outcome == "recorded":
            print(f"recorded {detail['name']} v{detail['version']} ({detail['review']['verdict']}) "
                  f"on attempt {tries}")
            return 0
        if outcome == "already":
            print(f"already recorded {detail['name']} v{detail.get('version')}; nothing to do")
            return 0
        last = detail
        elapsed = time.monotonic() - started
        delay = random.uniform(args.base_delay / 2, min(args.max_delay, args.base_delay * 2 ** (tries - 1)))
        if tries >= args.attempts or elapsed + delay > args.deadline:
            break
        print(f"attempt {tries}: main moved or push failed; retrying in {delay:.1f}s", file=sys.stderr)
        time.sleep(delay)
    cmd = recovery_command(args, remote_shown)
    print(f"STRANDED  {args.path}: merged but no review record after {tries} attempts "
          f"({time.monotonic() - started:.0f}s); last error: {last}\n"
          f"It will not be published until recorded. Recover with (README 'Recovering'):\n{cmd}",
          file=sys.stderr)
    write_report(args.report, (
        f"This submission was merged, but its review record **could not be pushed** after {tries} "
        f"attempts, so it is **not published yet**. Nothing is lost: a maintainer can record it "
        f"(README, \"Recovering\") with:\n\n```bash\n{cmd}\n```\n"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
