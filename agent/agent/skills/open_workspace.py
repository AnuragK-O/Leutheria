from agent.skills import dev_common as dc


async def run(args: dict, websocket, pending: dict) -> dict:
    """Open an existing folder in a code editor, and say what branch it is
    on if it is a repository. All safe tools: nothing asks."""
    steps = dc.Steps(websocket, pending)
    path = dc.text_arg(args, "path")
    app = dc.text_arg(args, "app", dc.DEFAULT_EDITOR)
    if not path:
        return steps.fail("I need the folder to open.")

    listing = await steps.run("list_files", {"path": path})
    if not listing.get("ok"):
        return steps.step_failed("finding the folder", listing)
    folder = dc.expand(path)

    failure = await dc.open_in_app(steps, folder, app)
    if failure:
        return failure

    message = f"Opened {folder.name or folder} in {app}."
    if ".git" in listing.get("files", []):
        status = await steps.run("git_status", {"path": str(folder), "log_count": 1})
        if not status.get("ok"):
            return steps.step_failed("reading the git status (the folder is open)", status)
        message += " " + status.get("message", "")
    return steps.done(message.strip(), path=str(folder), app=app)
