async def run(args: dict, websocket, pending: dict) -> dict:
    """A spoken overview of one folder: how many loose files of each kind,
    how many subfolders, the total size, and the newest file.

    Steps: find_files (top-level files) -> folder_size (total, and the
    subfolders). Read-only.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    folder = common.resolve(args.get("path"))
    if folder is None:
        return log.refuse("folder_summary needs the folder to describe")

    found = await log.run(
        "find_files",
        {"path": str(folder), "recursive": False, "sort": "modified", "limit": common.MAX_FILES_CONSIDERED},
    )
    if common.failed(found):
        return log.fail("find_files", found, f"reading {folder.name}")
    sized = await log.run("folder_size", {"path": str(folder), "limit": 50})
    if common.failed(sized):
        return log.fail("folder_size", sized, f"measuring {folder.name}")

    files = found.get("files", [])
    counts = {}
    for file in files:
        key = (common.category_of(file["name"]) or common.OTHER)[0]
        counts[key] = counts.get(key, 0) + 1
    subfolders = sum(1 for item in sized.get("items", []) if item.get("kind") == "folder")
    total_files = found.get("total_matched", len(files))

    qualifier = "at least " if sized.get("incomplete") else ""
    if not files and not subfolders:
        message = f"{folder.name} is empty."
    else:
        message = (
            f"{folder.name} is {qualifier}{sized.get('total')}, with {common.plural(total_files, 'loose file')} "
            f"and {common.plural(subfolders, 'folder')}."
        )
        if counts:
            message += f" The files are {common.summarise(counts, common.describe_category)}."
        if files:
            message += f" The newest is {files[0]['name']}, from {common.spoken_when(files[0]['modified'])}."
    return log.done(
        message,
        path=str(folder),
        total=sized.get("total"),
        loose_files=total_files,
        folders=subfolders,
        by_group=counts,
        newest=files[0]["name"] if files else None,
    )
