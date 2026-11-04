"""Commit a built, signed index onto main, or step aside for a newer build.

    commit_index.py --repo <clone> --built-sha <sha> --index <index.json> --sig <index.json.sig>
                    [--attempts 10] [--base-delay 2] [--max-delay 30]

publish.yml's commit job runs this after verifying the signed index. <repo>
is a clone of the registry whose `origin` it can push to; its working tree is
moved onto main (only index.json and index.json.sig are written). Each try
fetches main and compares it with <built-sha>, the commit the index was built
from:

- main's INPUTS (below) differ from <built-sha>'s: a change landed after this
  build, so this index would miss it. Stop, exit 0, "superseded". Every change
  to an input is followed by a publish of its own that reads main later --
  submission and yank call publish after their push, and a push by hand to
  those paths triggers it (publish.yml's `paths` filter is this same list) --
  so the newest change is always indexed by the run it started.
- otherwise the inputs are identical, so the index is correct for main as it
  is now, whatever else moved (an index commit, the README): if main already
  has the same index at least as new, nothing to do; else write the two files
  on top of main, commit, push as a fast-forward. A rejected push is retried
  from a fresh fetch.

This is what keeps a burst from losing the final rebuild: no run can publish
an index older than main's inputs, and no run steps aside unless a later one
is coming. It holds with or without the workflow's concurrency group, which
only saves runner time by coalescing queued runs.

Exit 0 committed, unchanged or superseded; 1 gave up (loud: the job fails).
"""

import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

# What build_index.py reads (plus keys/, which verify_index.py reads). Keep in
# step with publish.yml's on.push.paths; registry_pipeline_check.py checks it.
INPUTS = ("skills", "reviews", "yanked.json", "LEUTHERIA_REF", "tools", "keys")
FILES = ("index.json", "index.json.sig")
STAMPS = ("generated_at", "expires_at")
GIT_TIMEOUT = 120


def git(repo: Path, *args, check=True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()[-300:]}")
    return r


def _same_index_not_older(current: bytes, ours: bytes) -> bool:
    """main already carries this index, generated no earlier than ours."""
    try:
        a, b = json.loads(current), json.loads(ours)
        blank = dict.fromkeys(STAMPS)
        return {**a, **blank} == {**b, **blank} and str(a.get("generated_at", "")) >= str(b.get("generated_at", ""))
    except (ValueError, TypeError, AttributeError):
        return False


def attempt(repo: Path, built: str, payload: dict) -> str:
    git(repo, "fetch", "--quiet", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    main = git(repo, "rev-parse", "refs/remotes/origin/main").stdout.strip()
    if main != built and git(repo, "diff", "--quiet", built, main, "--", *INPUTS, check=False).returncode != 0:
        return "superseded"
    # main's committed copy, not the working tree: the build left its own
    # (untracked) index.json there when main has none yet.
    current = subprocess.run(["git", "-C", str(repo), "show", f"{main}:index.json"],
                             capture_output=True, timeout=GIT_TIMEOUT)
    has_sig = git(repo, "cat-file", "-e", f"{main}:index.json.sig", check=False).returncode == 0
    if current.returncode == 0 and has_sig and _same_index_not_older(current.stdout, payload["index.json"]):
        return "unchanged"
    git(repo, "checkout", "--quiet", "--force", "-B", "publish-index", main)
    for name, data in payload.items():
        (repo / name).write_bytes(data)
    git(repo, "add", *FILES)
    if git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return "unchanged"
    git(repo, "commit", "--quiet", "-m", "Publish index")
    if git(repo, "push", "--quiet", "origin", "HEAD:refs/heads/main", check=False).returncode == 0:
        return "committed"
    return "lost"


def main() -> int:
    parser = argparse.ArgumentParser(description="Commit the signed index onto main, or step aside for a newer build")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--built-sha", required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--sig", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--base-delay", type=float, default=2.0)
    parser.add_argument("--max-delay", type=float, default=30.0)
    args = parser.parse_args()
    # Read both now: moving the tree onto main may replace copies in <repo>.
    payload = {"index.json": args.index.read_bytes(), "index.json.sig": args.sig.read_bytes()}
    last = ""
    for tries in range(1, args.attempts + 1):
        try:
            outcome = attempt(args.repo, args.built_sha, payload)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
            outcome, last = "lost", str(e)
        if outcome == "superseded":
            print(f"superseded: main's inputs changed after {args.built_sha[:12]}; the publish that change "
                  f"started will index it")
            return 0
        if outcome in ("committed", "unchanged"):
            print(f"index {outcome}")
            return 0
        if tries < args.attempts:
            time.sleep(random.uniform(args.base_delay / 2, min(args.max_delay, args.base_delay * 2 ** (tries - 1))))
    print(f"FAIL  could not commit the index after {args.attempts} attempts: {last or 'push rejected'}. "
          f"Run publish again by hand.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
