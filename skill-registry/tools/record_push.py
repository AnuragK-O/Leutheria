"""Write a merged submission's review record onto main and push it, retrying until it lands.

    record_push.py --remote <url> --work <dir> --path skills/<name>/<hash>.json
                   --verdict auto_approved|human_approved --author <login>
                   [--pr <n>] [--screen <result.json>] [--report <file.md>]
                   [--attempts 60] [--deadline 1800] [--base-delay 2] [--max-delay 60]

submission.yml runs this right after `gh pr merge`; a maintainer runs the same
command to recover a submission that was merged but never recorded (README,
"Recovering"). <work> is this tool's own clone of <remote> (main_push.py): never
point it at a checkout you care about.

Each attempt (main_push.push_to_main) syncs <work> to main exactly, runs
record_review.record() on that tree (name ownership re-checked, version =
highest recorded + 1, both against main as it is now), commits, and pushes as a
plain fast-forward. The push is accepted only if main hasn't moved since the
fetch, i.e. only if the version and owner check were computed from exactly the
commit the record lands on; otherwise it starts again from the new main after a
jittered exponential backoff.

Re-running is safe: if main already has this file's record by this author, that
is success ("already recorded"), not an error.

--report: written with the recovery command BEFORE the first attempt, replaced
on exit 1 or 2, and removed on exit 0. So it exists exactly when the record is
not known to be on main -- including when the job is cancelled or killed
mid-retry, which is what lets the workflow report a stranded merge even then.

Exit 0  recorded, or already recorded.
Exit 2  refused for good, not retried: the name has a record by another author
        on main, the skill file isn't on main, or main has an unreadable or
        foreign record for this file. The file stays on main unindexed.
Exit 1  gave up (attempts or deadline used up, or git kept failing). Merged but
        STRANDED: never indexed until recorded.
"""

import argparse
import sys
from pathlib import Path

import common
import main_push
import record_review
from main_push import ALREADY, GaveUp, Permanent, display_remote  # noqa: F401  (display_remote: re-exported)


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


def recovery_command(args) -> str:
    parts = ["python tools/record_push.py", f"--remote {display_remote(args.remote)}", "--work /tmp/record-review",
             f"--path {args.path}", f"--verdict {args.verdict}", f"--author {args.author}"]
    if args.pr is not None:
        parts.append(f"--pr {args.pr}")
    parts.append("--screen screen.json  # from the run's artifact, if it has one")
    return " \\\n    ".join(parts)


def recovery_block(args) -> str:
    return (f"A maintainer can record it (README, \"Recovering\"; safe to run even if it did land) with:\n\n"
            f"```bash\n{recovery_command(args)}\n```\n")


def write_report(path, text: str) -> None:
    if path:
        Path(path).write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a merged submission's review on main, retrying the push")
    main_push.add_retry_args(parser)
    parser.add_argument("--path", required=True)
    parser.add_argument("--verdict", required=True, choices=common.REVIEW_VERDICTS)
    parser.add_argument("--author", required=True)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--screen", type=Path)
    parser.add_argument("--published-at", default=None)
    parser.add_argument("--report", type=Path, help="markdown explanation; exists exactly while unresolved (above)")
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
    write_report(args.report, (
        "This submission was merged, but the run stopped (it was cancelled or killed) while writing its review "
        "record, so it may **not be published**: the index lists only recorded files.\n\n" + recovery_block(args)))

    def apply(work: Path):
        if already_recorded(work, args.path, args.author) is not None:
            return ALREADY
        try:
            return record_review.record(work, args.path, args.verdict, screen, args.author, args.pr,
                                        args.published_at or record_review.now_iso())
        except (common.RegistryError, OSError, ValueError) as e:
            raise Permanent(str(e))

    pr = f" (PR #{args.pr})" if args.pr is not None else ""
    try:
        outcome, rec = main_push.push_to_main(args.remote, args.work, apply, f"Review record for {args.path}{pr}",
                                              **main_push.retry_kwargs(args))
    except Permanent as e:
        print(f"REFUSED  {args.path}: {e}", file=sys.stderr)
        write_report(args.report, (
            f"This submission was merged, but its review record was **not written**: {e}.\n\n"
            f"The skill file stays on main without a record, so it is **not published** (the index "
            f"lists only recorded files). Retrying won't change this. A maintainer should decide "
            f"whether to remove `{args.path}` from main or leave it unpublished.\n"))
        return 2
    except GaveUp as e:
        print(f"STRANDED  {args.path}: merged but no review record: {e}\n"
              f"It will not be published until recorded. Recover with (README 'Recovering'):\n"
              f"{recovery_command(args)}", file=sys.stderr)
        write_report(args.report, (
            f"This submission was merged, but its review record **could not be pushed** after {e.tries} "
            f"attempts, so it is **not published yet**. Nothing is lost.\n\n" + recovery_block(args)))
        return 1
    if args.report:
        args.report.unlink(missing_ok=True)
    if outcome == "already":
        print(f"already recorded {args.path}; nothing to do")
    else:
        print(f"recorded {rec['name']} v{rec['version']} ({rec['review']['verdict']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
