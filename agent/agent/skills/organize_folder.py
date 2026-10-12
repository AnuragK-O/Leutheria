async def run(args: dict, websocket, pending: dict) -> dict:
    """Sort the loose files at the top of a folder into type subfolders
    (Images, Documents, Archives...) inside that same folder.

    Only regular, visible, top-level files of a recognised type move;
    folders, hidden files, links, unknown types and files still downloading
    stay put. Never overwrites (move_file refuses, and a taken name gets a
    " (2)" suffix), never deletes, at most files_common.MAX_FILES_PER_RUN
    files per run. dry_run does reads only and reports the plan.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    folder = common.resolve(args.get("path"))
    if folder is None:
        return log.refuse("organize_folder needs the folder to organise")
    dry_run = common.as_bool(args.get("dry_run"))

    refusal = await common.check_reorganisable(log, folder)
    if refusal:
        return refusal

    files, more_exist, error = await common.list_candidates(log, folder)
    if error:
        return error

    def classify(file):
        category = common.category_of(file["name"])
        return (folder / category[0], category[0]) if category else None

    return await common.move_into_folders(
        log,
        files,
        classify,
        describe=common.describe_category,
        into=f"into type folders in {folder.name}",
        dry_run=dry_run,
        more_exist=more_exist,
        nothing=f"There were no loose files of a known type to sort in {folder.name}.",
    )
