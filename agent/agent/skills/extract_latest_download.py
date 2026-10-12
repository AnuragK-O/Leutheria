ARCHIVE_EXTENSIONS = "zip,tar,gz,tgz,bz2,tbz2,xz,txz"
_STRIP = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".tar", ".zip")


async def run(args: dict, websocket, pending: dict) -> dict:
    """Unpack the most recently downloaded archive into a new folder next
    to it -- "unzip the thing I just downloaded".

    Steps: find_files (newest zip/tar at the top of Downloads) -> file_info
    (a folder name nothing occupies) -> extract_archive, which creates that
    folder itself, validates every entry against zip-slip, and refuses
    rather than writing into anything that exists. The archive is kept.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    folder = common.resolve(args.get("folder"), common.downloads())

    found = await log.run(
        "find_files",
        {"path": str(folder), "extension": ARCHIVE_EXTENSIONS, "recursive": False, "sort": "modified", "limit": 1},
    )
    if common.failed(found):
        return log.fail("find_files", found, f"looking in {folder.name}")
    files = [f for f in found.get("files", []) if not common.in_progress(f["name"])]
    if not files:
        return log.refuse(f"there is no zip or tar archive in {folder.name}")
    archive = files[0]

    stem = archive["name"]
    for suffix in _STRIP:
        if stem.lower().endswith(suffix) and len(stem) > len(suffix):
            stem = stem[: -len(suffix)]
            break
    else:
        stem = f"{stem} extracted"

    destination, error = await common.free_path(log, folder, stem)
    if error:
        return error

    extracted = await log.run("extract_archive", {"archive": archive["path"], "destination": str(destination)})
    if common.failed(extracted):
        return log.fail("extract_archive", extracted, f"unpacking {archive['name']}")

    message = (
        f"Unpacked {archive['name']} into a new folder called {destination.name} in {folder.name}: "
        f"{common.plural(extracted.get('file_count', 0), 'file')}."
    )
    if extracted.get("skipped"):
        message += f" {common.plural(extracted['skipped'], 'link')} inside it were left out."
    return log.done(
        message, path=extracted.get("path"), archive=archive["path"], file_count=extracted.get("file_count")
    )
