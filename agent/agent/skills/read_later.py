from agent.skills import dev_common as dc

DEFAULT_PATH = "~/Notes/read-later.md"


async def run(args: dict, websocket, pending: dict) -> dict:
    """Add a link to a running reading list (one line per link). Only the
    address is kept, nothing is fetched. Uses the URL on the clipboard when
    none is given. append_text can only add, so nothing asks."""
    steps = dc.Steps(websocket, pending)
    url, from_clipboard, failure = await dc.url_or_clipboard(steps, args)
    if failure:
        return failure

    list_path = dc.text_arg(args, "list_path", DEFAULT_PATH)
    title = " ".join(dc.text_arg(args, "title").split())[:200]
    line = f"- [{dc.stamp('%Y-%m-%d')}] " + (f"{title}: {url}" if title else url)
    result = await steps.run("append_text", {"path": list_path, "text": line})
    if not result.get("ok"):
        return steps.step_failed("adding it to the list", result)

    what = title or url
    source = " from the clipboard" if from_clipboard else ""
    return steps.done(f"Added {what}{source} to your reading list at {list_path}.", url=url, path=list_path)
