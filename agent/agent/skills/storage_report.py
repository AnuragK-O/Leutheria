FOLDERS = ("Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures")


async def run(args: dict, websocket, pending: dict) -> dict:
    """How full the disk is, and which of the main home folders are using it.

    Steps: get_disk_space -> list_files on the home folder (which of the
    usual folders exist) -> folder_size for each. Read-only. Each
    folder_size is bounded, so on a very large folder the figure is a lower
    bound and is reported as "at least".
    """
    from agent.skills import files_common as common
    from agent.tools.fs_common import human_size

    log = common.StepLog(websocket, pending)

    disk = await log.run("get_disk_space", {})
    if common.failed(disk):
        return log.fail("get_disk_space", disk, "reading the disk")

    home = common.home()
    listing = await log.run("list_files", {"path": str(home)})
    if common.failed(listing):
        return log.fail("list_files", listing, "reading the home folder")
    present = [name for name in FOLDERS if name in listing.get("files", [])]

    sizes = []
    for name in present:
        sized = await log.run("folder_size", {"path": str(home / name), "limit": 3})
        if common.failed(sized):
            # Pictures and Music can be closed to the agent by macOS privacy
            # settings; one unreadable folder shouldn't sink the report.
            if "not a folder" in str(sized.get("error", "")):
                continue
            return log.fail("folder_size", sized, f"measuring {name}")
        sizes.append(
            {
                "name": name,
                "bytes": sized.get("total_bytes", 0),
                "size": sized.get("total"),
                "incomplete": bool(sized.get("incomplete")),
            }
        )

    sizes.sort(key=lambda item: -item["bytes"])
    message = f"The disk has {disk.get('free_gb')} gigabytes free of {disk.get('total_gb')}."
    if sizes:
        spoken = ", ".join(
            f"{item['name']} {'at least ' if item['incomplete'] else ''}{item['size']}" for item in sizes[:3]
        )
        message += f" Of your main folders, the biggest are {spoken}."
    return log.done(
        message,
        free_gb=disk.get("free_gb"),
        total_gb=disk.get("total_gb"),
        folders=[{"name": i["name"], "size": i["size"], "incomplete": i["incomplete"]} for i in sizes],
        measured=human_size(sum(item["bytes"] for item in sizes)),
    )
