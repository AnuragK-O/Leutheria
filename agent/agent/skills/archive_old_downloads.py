ARCHIVE_FOLDER = "Old Downloads"
DEFAULT_DAYS = 30


async def run(args: dict, websocket, pending: dict) -> dict:
    """Move files that have sat in Downloads untouched for `days` days into
    "Downloads/Old Downloads/<YYYY-MM>", by the month they were last modified.

    Moves, never deletes -- "archive" here means out of the way, not gone.
    Top-level regular files only; never overwrites; capped per run; dry_run
    reads only.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    downloads = common.downloads()
    days = common.as_int(args.get("days"), DEFAULT_DAYS, 1, 3650)
    dry_run = common.as_bool(args.get("dry_run"))
    target = downloads / ARCHIVE_FOLDER

    files, more_exist, error = await common.list_candidates(log, downloads, older_than_days=days, sort="oldest")
    if error:
        return error

    def classify(file):
        month = str(file.get("modified", ""))[:7]
        return (target / month, month) if len(month) == 7 else None

    result = await common.move_into_folders(
        log,
        files,
        classify,
        describe=common.describe_month,
        into=f"older than {days} days from Downloads into {ARCHIVE_FOLDER}",
        dry_run=dry_run,
        more_exist=more_exist,
        nothing=f"Nothing in Downloads is older than {days} days.",
    )
    if result.get("ok"):
        result["destination"] = str(target)
        result["days"] = days
    return result
