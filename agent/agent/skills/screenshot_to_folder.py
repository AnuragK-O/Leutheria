import re

_UNSAFE_NAME = re.compile(r"[/\\:\x00-\x1f]")


async def run(args: dict, websocket, pending: dict) -> dict:
    """Take a screenshot now and file it in a Screenshots folder (by
    default "~/Pictures/Screenshots") instead of on the Desktop.

    Steps: create_folder -> file_info (a free name; take_screenshot would
    write over an existing file, so the name is checked first and is
    timestamped to the second) -> take_screenshot.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    folder = common.resolve(args.get("folder"), common.pictures() / "Screenshots")
    label = _UNSAFE_NAME.sub(" ", str(args.get("name") or "")).strip().strip(".")[:80]
    stamp = common.now().strftime("%Y-%m-%d at %H.%M.%S")
    filename = f"{label} {stamp}.png" if label else f"Screenshot {stamp}.png"

    made = await log.run("create_folder", {"path": str(folder)})
    if common.failed(made):
        return log.fail("create_folder", made, f"creating {folder.name}")

    path, error = await common.free_path(log, folder, filename)
    if error:
        return error

    shot = await log.run("take_screenshot", {"save_path": str(path)})
    if common.failed(shot):
        return log.fail("take_screenshot", shot, "taking the screenshot")

    return log.done(f"Screenshot saved as {path.name} in {folder.name}.", path=shot.get("path", str(path)))
