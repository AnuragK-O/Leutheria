async def run(args: dict, websocket, pending: dict) -> dict:
    """Sort the loose files at the top of a folder into one subfolder per
    month they were last modified in (2026-09, 2026-10...).

    Same guarantees as organize_folder -- it is the same bulk routine with a
    different rule for picking the destination: top-level regular files
    only, never overwrites, never deletes, capped per run, dry_run reads only.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    folder = common.resolve(args.get("path"))
    if folder is None:
        return log.refuse("organize_by_date needs the folder to organise")
    dry_run = common.as_bool(args.get("dry_run"))

    refusal = await common.check_reorganisable(log, folder)
    if refusal:
        return refusal

    files, more_exist, error = await common.list_candidates(log, folder)
    if error:
        return error

    def classify(file):
        month = str(file.get("modified", ""))[:7]  # "YYYY-MM" of "YYYY-MM-DD HH:MM"
        return (folder / month, month) if len(month) == 7 else None

    return await common.move_into_folders(
        log,
        files,
        classify,
        describe=common.describe_month,
        into=f"into month folders in {folder.name}",
        dry_run=dry_run,
        more_exist=more_exist,
        nothing=f"There were no loose files to sort in {folder.name}.",
    )
