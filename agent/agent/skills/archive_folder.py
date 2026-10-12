async def run(args: dict, websocket, pending: dict) -> dict:
    """Zip a folder into "<name>.zip" next to it (or in `destination_folder`),
    picking a free name if that one is taken.

    Steps: file_info (is it a folder) -> file_info (a free zip name) ->
    zip_folder. The original folder is only read; zip_folder creates the
    archive exclusively, so nothing is overwritten even if a file appears at
    the chosen name between the check and the write.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    source = common.resolve(args.get("path"))
    if source is None:
        return log.refuse("archive_folder needs the folder to zip")

    info = await log.run("file_info", {"path": str(source)})
    if common.failed(info):
        return log.fail("file_info", info, "checking the folder")
    if not info.get("exists"):
        return log.refuse(f"{source} does not exist")
    if info.get("kind") != "folder":
        return log.refuse(f"{source} is a {info.get('kind')}, not a folder")

    where = common.resolve(args.get("destination_folder"), source.parent)
    if where == source or source in where.parents:
        return log.refuse("the zip file cannot go inside the folder it is zipping")
    destination, error = await common.free_path(log, where, f"{source.name}.zip")
    if error:
        return error

    zipped = await log.run("zip_folder", {"source": str(source), "destination": str(destination)})
    if common.failed(zipped):
        return log.fail("zip_folder", zipped, f"zipping {source.name}")

    return log.done(
        f"Zipped {source.name} into {destination.name}: "
        f"{common.plural(zipped.get('file_count', 0), 'file')}, {zipped.get('size')}. The original folder is untouched.",
        path=zipped.get("path"),
        file_count=zipped.get("file_count"),
        size=zipped.get("size"),
    )
