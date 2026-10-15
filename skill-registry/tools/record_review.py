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
Refuses to overwrite an existing record. Exit 0 / 1.
"""

import argparse
import datetime
import sys
from pathlib import Path

import common

MAX_FLAGS = 20


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
        "screened_by": str(screen.get("model") or "none")[:100],
        "screen_version": version if isinstance(version, int) and not isinstance(version, bool) else 0,
    }


def record(repo: Path, rel: str, verdict: str, screen, author: str, pr, published_at: str) -> dict:
    if verdict not in common.REVIEW_VERDICTS:
        raise common.RegistryError(f"verdict must be one of {common.REVIEW_VERDICTS}")
    name, digest = common.split_skill_path(rel)
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
    args = parser.parse_args()
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
