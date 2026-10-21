"""Yank a published version on main, retrying until it lands (the yank workflow's step).

    yank_push.py --remote <url> --work <dir> --name <name> --content-hash <sha256>
                 --reason <text> [--by <login>]
                 [--attempts 60] [--deadline 1800] [--base-delay 2] [--max-delay 60]

A yank is the security response for withdrawing a bad skill, so it must not fail
just because review records are landing at the same moment. Each attempt
(main_push.push_to_main) syncs <work> (this tool's own clone) to main exactly,
applies yank.yank() to that tree, commits yanked.json and pushes as a plain
fast-forward; a rejected push starts again from the new main after a jittered
exponential backoff.

Idempotent: a version already in yanked.json on main is success ("already
yanked"), so a push that landed but looked failed, or a re-run, is harmless.

Exit 0 yanked or already yanked; 2 refused for good (bad name or hash, or the
version isn't published); 1 gave up -- run the workflow again.
"""

import argparse
import sys
from pathlib import Path

import common
import main_push
import yank
from main_push import ALREADY, GaveUp, Permanent
from record_review import now_iso


def is_yanked(work: Path, name: str, digest: str) -> bool:
    try:
        doc = common.read_json(work / "yanked.json", default={"yanked": []})
        return any(y["name"] == name and y["content_hash"] == digest for y in doc["yanked"])
    except (OSError, ValueError, KeyError, TypeError):
        raise Permanent("yanked.json on main is unreadable")


def main() -> int:
    parser = argparse.ArgumentParser(description="Yank a published skill version on main, retrying the push")
    main_push.add_retry_args(parser)
    parser.add_argument("--name", required=True)
    parser.add_argument("--content-hash", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--by", default="maintainer")
    args = parser.parse_args()

    def apply(work: Path):
        if is_yanked(work, args.name, args.content_hash):
            return ALREADY
        try:
            yank.yank(work, args.name, args.content_hash, args.reason, args.by, now_iso())
        except (common.RegistryError, OSError, ValueError) as e:
            raise Permanent(str(e))
        return True

    try:
        outcome, _ = main_push.push_to_main(args.remote, args.work, apply,
                                            f"Yank {args.name} {args.content_hash[:12]}",
                                            **main_push.retry_kwargs(args))
    except Permanent as e:
        print(f"REFUSED  {e}", file=sys.stderr)
        return 2
    except GaveUp as e:
        print(f"FAIL  yank of {args.name}/{args.content_hash[:12]} not recorded: {e}. Run the yank workflow again.",
              file=sys.stderr)
        return 1
    print(f"{'already yanked' if outcome == 'already' else 'yanked'} {args.name}/{args.content_hash[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
