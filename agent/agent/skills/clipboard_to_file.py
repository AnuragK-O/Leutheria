from agent.skills import dev_common as dc

DEFAULT_FOLDER = "~/Notes"


async def run(args: dict, websocket, pending: dict) -> dict:
    """Write what's on the clipboard to a brand-new file. write_new_file
    refuses to replace an existing file, so this can't lose anything; with
    no path given the name carries a timestamp and can't collide."""
    steps = dc.Steps(websocket, pending)
    path = dc.text_arg(args, "path")
    if path:
        target = dc.expand(path)
    else:
        target = dc.expand(DEFAULT_FOLDER) / f"clipboard-{dc.stamp('%Y%m%d-%H%M%S')}.txt"
    if target.exists():
        return steps.fail(f"{target} already exists. I never overwrite a file, so give a different name.")

    clip = await steps.run("get_clipboard", {})
    if not clip.get("ok"):
        return steps.step_failed("reading the clipboard", clip)
    text = clip.get("text") or ""
    if not text.strip():
        return steps.fail("The clipboard is empty, or holds something that is not text, so no file was made.")

    result = await steps.run("create_folder", {"path": str(target.parent)})
    if not result.get("ok"):
        return steps.step_failed("creating the folder", result)
    content = text if text.endswith("\n") else text + "\n"
    result = await steps.run("write_new_file", {"path": str(target), "content": content})
    if not result.get("ok"):
        return steps.step_failed("writing the file", result)

    app = dc.text_arg(args, "open_in")
    if app:
        failure = await dc.open_in_app(steps, target, app, target)
        if failure:
            return failure
    return steps.done(
        f"Saved the clipboard, {len(text)} characters, to {target}." + (f" Opened it in {app}." if app else ""),
        path=str(target),
        characters=len(text),
    )
