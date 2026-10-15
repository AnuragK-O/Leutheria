"""Is a maintainer's approval bound to the exact commit being merged?

    human_approval.py candidates --reviews <reviews.json> --head-sha <sha> --author <login>
    human_approval.py verify     --reviews <reviews.json> --head-sha <sha> --author <login>
                                 --permissions <perms.txt> --out <result.json>

A label alone is not an approval: anyone with triage rights can add one, and
a label added for one commit would still be there after the submitter pushes
another. So the workflow treats the `human-approved` (or
`human-approved-override`) label only as "please check now", and this script
decides whether there is an approval to act on:

- --reviews is the PR's review list as GitHub's API returns it
  (`gh api --paginate --slurp repos/<r>/pulls/<n>/reviews`: a list, or a list
  of pages);
- each reviewer's latest decisive review (APPROVED, CHANGES_REQUESTED or
  DISMISSED; COMMENTED and PENDING don't change it) counts, and only if it is
  APPROVED and its commit_id is exactly --head-sha;
- the PR's author never counts;
- `candidates` prints the logins left (one per line, each a valid GitHub
  login, so the workflow can safely look up their permission);
- `verify` reads "<login> <permission>" lines (from
  `gh api repos/<r>/collaborators/<login>/permission --jq .permission`) and
  approves only if a candidate has admin, maintain or write.

Writes {"approved": bool, "by": [logins], "reason": str}. Exit 0 when it
could decide (approved or not), 2 on a usage error.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import common

LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DECISIVE = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
MERGERS = {"admin", "maintain", "write"}


def _flatten(reviews) -> list:
    if not isinstance(reviews, list):
        raise ValueError("reviews must be a list")
    out = []
    for item in reviews:
        if isinstance(item, list):
            out.extend(item)
        else:
            out.append(item)
    return [r for r in out if isinstance(r, dict)]


def candidates(reviews, head_sha: str, author: str) -> list:
    if not SHA_RE.fullmatch(head_sha or ""):
        raise ValueError("head sha must be 40 hex characters")
    latest = {}
    for r in sorted(_flatten(reviews), key=lambda r: (str(r.get("submitted_at") or ""), r.get("id") or 0)):
        login = (r.get("user") or {}).get("login")
        if not isinstance(login, str) or r.get("state") not in DECISIVE:
            continue
        latest[login] = r
    out = []
    for login, r in sorted(latest.items()):
        if (r.get("state") == "APPROVED" and r.get("commit_id") == head_sha
                and login.lower() != (author or "").lower() and LOGIN_RE.fullmatch(login)):
            out.append(login)
    return out


def verify(reviews, head_sha: str, author: str, permissions: dict) -> dict:
    found = candidates(reviews, head_sha, author)
    by = [login for login in found if permissions.get(login) in MERGERS]
    if by:
        return {"approved": True, "by": by, "reason": f"approved at {head_sha[:12]} by {', '.join(by)}"}
    if found:
        return {"approved": False, "by": [], "reason": "the approving reviewers don't have write access"}
    return {"approved": False, "by": [],
            "reason": f"no approving review on the current head {head_sha[:12]} from someone other than the author"}


def read_permissions(path: Path) -> dict:
    perms = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and LOGIN_RE.fullmatch(parts[0]):
            perms[parts[0]] = parts[1]
    return perms


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a maintainer approval against the head commit")
    parser.add_argument("cmd", choices=("candidates", "verify"))
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--permissions", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        reviews = common.read_json(args.reviews)
        if args.cmd == "candidates":
            for login in candidates(reviews, args.head_sha, args.author):
                print(login)
            return 0
        if args.permissions is None or args.out is None:
            parser.error("verify needs --permissions and --out")
        result = verify(reviews, args.head_sha, args.author, read_permissions(args.permissions))
    except (OSError, ValueError) as e:
        result = {"approved": False, "by": [], "reason": f"could not check the approval: {e}"}
        if args.cmd == "candidates":
            print(f"FAIL  {e}", file=sys.stderr)
            return 2
    common.write_json(args.out, result)
    print(("APPROVED  " if result["approved"] else "NOT APPROVED  ") + result["reason"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
