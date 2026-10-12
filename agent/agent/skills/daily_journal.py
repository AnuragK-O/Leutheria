async def run(args: dict, websocket, pending: dict) -> dict:
    """Start today's journal file if there isn't one, and optionally add a
    timestamped entry to it.

    One Markdown file per day: "~/Documents/Journal/YYYY-MM-DD.md". Steps:
    file_info (is today's file there) -> write_new_file (only if it isn't;
    create-only, so an existing day is never replaced) -> append_text (only
    if there is an entry; append-only) -> open_file, when that tool exists
    and `open` isn't false. Opening is a convenience: if it fails, the
    journal is still written and the message says it wasn't opened.
    """
    from agent.skills import files_common as common
    from agent.tools.registry import TOOLS

    log = common.StepLog(websocket, pending)
    folder = common.resolve(args.get("journal_dir"), common.documents() / "Journal")
    today = common.now()
    path = folder / f"{today.strftime('%Y-%m-%d')}.md"
    entry = " ".join(str(args.get("text") or "").split())

    info = await log.run("file_info", {"path": str(path)})
    if common.failed(info):
        return log.fail("file_info", info, "checking for today's journal")
    if info.get("exists") and info.get("kind") != "file":
        return log.refuse(f"{path} exists but is not a file")

    created = not info.get("exists")
    if created:
        header = f"# {today.strftime('%A')}, {today.strftime('%B')} {today.day}, {today.year}\n\n"
        written = await log.run("write_new_file", {"path": str(path), "content": header})
        if common.failed(written):
            return log.fail("write_new_file", written, "creating today's journal")

    if entry:
        line = f"- {today.strftime('%H:%M')} {entry}"
        appended = await log.run("append_text", {"path": str(path), "text": line})
        if common.failed(appended):
            return log.fail("append_text", appended, "adding the entry")

    opened = None
    if common.as_bool(args.get("open"), True) and "open_file" in TOOLS:
        result = await log.run("open_file", {"path": str(path)})
        opened = not common.failed(result)

    if created and entry:
        message = "Started today's journal and added your entry."
    elif created:
        message = "Started today's journal."
    elif entry:
        message = "Added that to today's journal."
    else:
        message = "Today's journal already exists; nothing was added."
    if opened is True:
        message += " It's open now."
    elif opened is False:
        message += " I couldn't open it on screen."
    else:
        message += f" It's saved as {path.name} in {folder.name}; it is not open on screen."
    return log.done(message, path=str(path), created=created, entry_added=bool(entry), opened=bool(opened))
