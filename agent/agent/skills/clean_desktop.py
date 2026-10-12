CLEANUP_FOLDER = "Desktop Cleanup"


async def run(args: dict, websocket, pending: dict) -> dict:
    """Clear the loose files off the Desktop into "Desktop Cleanup/<type>".

    Unlike organize_folder, every loose file moves (unknown types go to
    Other, screenshots to Screenshots) -- the point is an empty Desktop with
    everything still findable in one place. Folders, hidden files, links and
    files still downloading stay. Never overwrites, never deletes, capped
    per run; dry_run reads only.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    desktop = common.desktop()
    dry_run = common.as_bool(args.get("dry_run"))
    target = desktop / CLEANUP_FOLDER

    files, more_exist, error = await common.list_candidates(log, desktop)
    if error:
        return error

    def classify(file):
        if common.is_screenshot(file["name"]):
            folder = common.SCREENSHOTS[0]
        else:
            folder = (common.category_of(file["name"]) or common.OTHER)[0]
        return target / folder, folder

    result = await common.move_into_folders(
        log,
        files,
        classify,
        describe=common.describe_category,
        into=f"off the Desktop into {CLEANUP_FOLDER}",
        dry_run=dry_run,
        more_exist=more_exist,
        nothing="The Desktop has no loose files to clear.",
    )
    if result.get("ok"):
        result["destination"] = str(target)
    return result
