"""Withdraw a published version (the yank workflow's script).

    yank.py --repo <dir> --name <name> --content-hash <sha256> --reason <text> [--by <login>]

Adds {name, content_hash, reason, yanked_at, by} to yanked.json. The file
stays (installed copies must still verify); the next index marks it
`yanked: true`, which hides it from Discover and shows installed copies as
withdrawn. Refuses a version that isn't published or is already yanked.
"""

import argparse
import sys
from pathlib import Path

import common
from record_review import now_iso


def yank(repo: Path, name: str, digest: str, reason: str, by: str, at: str) -> None:
    if not common.NAME_RE.fullmatch(name):
        raise common.RegistryError(f"invalid skill name {name!r}")
    if not common.HASH_RE.fullmatch(digest):
        raise common.RegistryError("content hash must be 64 lowercase hex chars")
    if not (repo / "reviews" / name / f"{digest}.json").is_file():
        raise common.RegistryError(f"{name}/{digest[:12]} is not published")
    path = repo / "yanked.json"
    doc = common.read_json(path, default={"yanked": []})
    if any(y["name"] == name and y["content_hash"] == digest for y in doc["yanked"]):
        raise common.RegistryError(f"{name}/{digest[:12]} is already yanked")
    doc["yanked"].append({"name": name, "content_hash": digest, "reason": common.summary_of(reason),
                          "yanked_at": at, "by": by})
    doc["yanked"].sort(key=lambda y: (y["name"], y["content_hash"]))
    common.write_json(path, doc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Yank a published skill version")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--content-hash", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--by", default="maintainer")
    args = parser.parse_args()
    try:
        yank(args.repo, args.name, args.content_hash, args.reason, args.by, now_iso())
    except (common.RegistryError, OSError, ValueError) as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 1
    print(f"yanked {args.name}/{args.content_hash[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
