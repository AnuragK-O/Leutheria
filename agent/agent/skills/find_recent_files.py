DEFAULT_DAYS = 3
PER_FOLDER_LIMIT = 20


async def run(args: dict, websocket, pending: dict) -> dict:
    """Find files added or changed in the last few days across Downloads,
    Desktop and Documents, newest first -- "where's that PDF I downloaded
    yesterday".

    Steps: one find_files per folder (each read-only and bounded), merged
    and sorted here. A folder that doesn't exist is skipped, not an error.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    days = common.as_int(args.get("days"), DEFAULT_DAYS, 1, 365)
    limit = common.as_int(args.get("limit"), 5, 1, 20)
    explicit = common.resolve(args.get("path"))
    folders = [explicit] if explicit else [common.downloads(), common.desktop(), common.documents()]

    query = {"modified_within_days": days, "sort": "modified", "limit": PER_FOLDER_LIMIT}
    if args.get("extension"):
        query["extension"] = args["extension"]
    if args.get("name"):
        query["name"] = str(args["name"])

    matches = []
    searched = []
    total = 0
    for folder in folders:
        if explicit is None:
            info = await log.run("file_info", {"path": str(folder)})
            if common.failed(info):
                return log.fail("file_info", info, f"checking {folder.name}")
            if info.get("kind") != "folder":
                continue
        found = await log.run("find_files", {"path": str(folder), **query})
        if common.failed(found):
            return log.fail("find_files", found, f"searching {folder.name}")
        searched.append(folder.name)
        total += found.get("total_matched", 0)
        matches.extend(found.get("files", []))

    if not searched:
        return log.refuse("none of Downloads, Desktop or Documents exist to search")

    matches.sort(key=lambda file: file.get("modified", ""), reverse=True)
    top = matches[:limit]
    what = "files"
    if args.get("extension"):
        what = f"{str(args['extension']).lstrip('.').upper()} files"
    if args.get("name"):
        what += f" named like {args['name']}"
    period = "the last day" if days == 1 else f"the last {days} days"
    places = " or ".join(searched) if len(searched) < 3 else ", ".join(searched[:-1]) + f" or {searched[-1]}"

    if not top:
        return log.done(f"No {what} changed in {period} in {places}.", files=[], total_matched=0)

    def where(file):
        return common.Path(file["path"]).parent.name

    first = top[0]
    message = f"The newest is {first['name']} in {where(first)}, from {common.spoken_when(first['modified'])}."
    if total > 1:
        others = ", ".join(file["name"] for file in top[1:3])
        message = f"{common.plural(total, 'match', 'matches')} from {period}. " + message
        if others:
            message += f" Then {others}."
    return log.done(
        message,
        files=[{"path": f["path"], "name": f["name"], "modified": f["modified"], "size": f["size"]} for f in top],
        total_matched=total,
    )
