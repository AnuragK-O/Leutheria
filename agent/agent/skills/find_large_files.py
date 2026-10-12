DEFAULT_MIN_MB = 50


async def run(args: dict, websocket, pending: dict) -> dict:
    """Say how big a folder is and name the largest individual files in it.

    Steps: folder_size (the total, and the biggest top-level items, which
    may be folders) -> find_files sorted by size (the biggest single files
    anywhere inside). Read-only: reports, changes nothing.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    folder = common.resolve(args.get("path"), common.downloads())
    limit = common.as_int(args.get("limit"), 5, 1, 20)
    try:
        min_mb = max(0.0, float(args.get("min_size_mb", DEFAULT_MIN_MB)))
    except (TypeError, ValueError):
        min_mb = DEFAULT_MIN_MB

    sized = await log.run("folder_size", {"path": str(folder), "limit": limit})
    if common.failed(sized):
        return log.fail("folder_size", sized, f"measuring {folder.name}")

    found = await log.run(
        "find_files", {"path": str(folder), "min_size_mb": min_mb, "sort": "size", "limit": limit}
    )
    if common.failed(found):
        return log.fail("find_files", found, f"searching {folder.name}")

    files = found.get("files", [])
    qualifier = "at least " if sized.get("incomplete") else ""
    message = f"{folder.name} holds {qualifier}{sized.get('total')}."
    if files:
        named = ", ".join(f"{file['name']} at {file['size']}" for file in files[:3])
        total = found.get("total_matched", len(files))
        message += f" {common.plural(total, 'file')} over {min_mb:g} megabytes. The biggest: {named}."
    else:
        message += f" No single file in it is over {min_mb:g} megabytes."
        items = sized.get("items", [])
        if items:
            message += f" The largest item is {items[0]['name']} at {items[0]['size']}."
    return log.done(
        message,
        path=str(folder),
        total=sized.get("total"),
        largest_files=[{"path": f["path"], "name": f["name"], "size": f["size"]} for f in files],
        largest_items=[{"name": i["name"], "kind": i["kind"], "size": i["size"]} for i in sized.get("items", [])],
    )
