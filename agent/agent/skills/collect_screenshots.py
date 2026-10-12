async def run(args: dict, websocket, pending: dict) -> dict:
    """Move the screenshots and screen recordings lying on the Desktop (or
    in `source`) into one Screenshots folder.

    Only files named like macOS screenshots ("Screenshot 2026-...", "Screen
    Recording ...") move; everything else is untouched. Never overwrites,
    never deletes, capped per run; dry_run reads only.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    source = common.resolve(args.get("source"), common.desktop())
    target = common.resolve(args.get("folder"), common.pictures() / "Screenshots")
    dry_run = common.as_bool(args.get("dry_run"))
    if target == source:
        return log.refuse("the screenshots folder must be a different folder from the one being cleared")

    files, more_exist, error = await common.list_candidates(log, source)
    if error:
        return error
    shots = [file for file in files if common.is_screenshot(file["name"])]

    result = await common.move_into_folders(
        log,
        shots,
        lambda file: (target, common.SCREENSHOTS[0]),
        describe=common.describe_category,
        into=f"from {source.name} into {target.name}",
        dry_run=dry_run,
        more_exist=more_exist,
        nothing=f"There are no screenshots in {source.name}.",
    )
    if result.get("ok"):
        result["destination"] = str(target)
    return result
