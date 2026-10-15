"""Write the review record for a merged submission and assign its version.

    record_review.py --repo <dir> --path skills/<name>/<hash>.json
                     --verdict auto_approved|human_approved [--screen <result.json>]
                     --author <login> [--pr <n>] [--published-at <iso>]

Writes reviews/<name>/<hash>.json:
    {"name", "content_hash", "version", "author", "published_at", "pr",
     "review": {"verdict", "risk", "flags", "screened_by", "screen_version"}}

`version` is the highest version already recorded for the name + 1, fixed
here once, so rebuilding the index can never renumber anything. A skill
file on main without a record is never indexed (build_index lists it as
pending), which is what keeps a file pushed around the PR flow unpublished.
Refuses to overwrite an existing record, and refuses when the name already
has a record by another author on --repo (main as it is now, not the PR's
base). --check-owner-only runs just that check (the workflow does it before
merging, and record() repeats it after). Exit 0 / 1.
"""

import argparse
import datetime
import sys
from pathlib import Path

import common

MAX_FLAGS = 20


def _screened_by(model) -> str:
    """screen.py's "model" is {"id", "called", "status", ...}: the model id,
    plus the status when the call wasn't a plain success (e.g. "stub:ok",
    "no_key", "skipped_static_block"), so the index never claims a screening
    that didn't happen. A bare string is taken as the id. Stringifying the
    dict put a Python repr in every index entry (BUGS.md #70)."""
    if isinstance(model, dict):
        name = str(model.get("id") or "none")
        status = model.get("status")
        if not model.get("called") or status != "ok":
            name += f" ({status or 'not called'})"
        return name[:100]
    return str(model or "none")[:100]


def review_block(verdict: str, screen) -> dict:
    screen = screen if isinstance(screen, dict) else {}
    flags = [common.flag_text(f) for f in ((screen.get("static") or {}).get("findings") or [])]
    flags += [common.flag_text(f) for f in (screen.get("findings") or [])]
    risk = screen.get("risk") if screen.get("risk") in common.RISKS else "high"
    version = screen.get("screen_version")
    return {
        "verdict": verdict,
        "risk": risk,
        "flags": [f for f in flags if f][:MAX_FLAGS],
        "screened_by": _screened_by(screen.get("model")),
        "screen_version": version if isinstance(version, int) and not isinstance(version, bool) else 0,
    }


def check_owner(repo: Path, name: str, author: str) -> None:
    """The name belongs to whoever has records for it on main *now*. The PR's
    structural check read the PR's base, which can be older than main: two
    PRs claiming one new name both pass it, so this re-check is what keeps
    the second from becoming "version 2" of the first's skill."""
    folder = repo / "reviews" / name
    if not folder.is_dir():
        return
    for p in folder.glob("*.json"):
        try:
            owner = common.read_json(p)["author"]
        except (OSError, ValueError, KeyError, TypeError):
            raise common.RegistryError(f"{p.relative_to(repo)} is unreadable; the name can't be claimed automatically")
        if not isinstance(owner, str) or owner.lower() != author.lower():
            raise common.RegistryError(f"{name!r} is already published by someone else")


def record(repo: Path, rel: str, verdict: str, screen, author: str, pr, published_at: str) -> dict:
    if verdict not in common.REVIEW_VERDICTS:
        raise common.RegistryError(f"verdict must be one of {common.REVIEW_VERDICTS}")
    name, digest = common.split_skill_path(rel)
    check_owner(repo, name, author)
    if not (repo / rel).is_file():
        raise common.RegistryError(f"{rel} is not in the repository")
    folder = repo / "reviews" / name
    target = folder / f"{digest}.json"
    if target.exists():
        raise common.RegistryError(f"{target.relative_to(repo)} already exists")
    versions = [common.read_json(p).get("version", 0) for p in folder.glob("*.json")] if folder.is_dir() else []
    entry = {
        "name": name,
        "content_hash": digest,
        "version": max(versions, default=0) + 1,
        "author": author,
        "published_at": published_at,
        "pr": pr,
        "review": review_block(verdict, screen),
    }
    common.write_json(target, entry)
    return entry


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a merged submission's review")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--verdict", required=True, choices=common.REVIEW_VERDICTS)
    parser.add_argument("--screen", type=Path)
    parser.add_argument("--author", required=True)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--published-at", default=None)
    parser.add_argument("--check-owner-only", action="store_true",
                        help="only check that --author may publish under this name on --repo (before merging)")
    args = parser.parse_args()
    if args.check_owner_only:
        try:
            name, _ = common.split_skill_path(args.path)
            check_owner(args.repo, name, args.author)
        except common.RegistryError as e:
            print(f"FAIL  {e}", file=sys.stderr)
            return 1
        print(f"ok  {args.author} may publish {name}")
        return 0
    screen = None
    if args.screen and args.screen.exists():
        try:
            screen = common.read_json(args.screen)
        except ValueError:
            screen = None
    try:
        entry = record(args.repo, args.path, args.verdict, screen, args.author, args.pr,
                       args.published_at or now_iso())
    except (common.RegistryError, OSError, ValueError) as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 1
    print(f"recorded {entry['name']} v{entry['version']} ({entry['review']['verdict']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
