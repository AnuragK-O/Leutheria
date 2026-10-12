from urllib.parse import urlsplit

from agent.skills import dev_common as dc

DEFAULT_FOLDER = "~/Notes/pages"
MAX_CHARS = 20000


async def run(args: dict, websocket, pending: dict) -> dict:
    """Fetch a web page's text and save it as a new Markdown file, so the
    content is kept even if the page changes or goes away. Uses the URL on
    the clipboard when none is given. All safe tools: nothing asks."""
    steps = dc.Steps(websocket, pending)
    url, _, failure = await dc.url_or_clipboard(steps, args)
    if failure:
        return failure

    page = await steps.run("fetch_url", {"url": url, "max_chars": MAX_CHARS})
    if not page.get("ok"):
        return steps.step_failed("fetching the page", page)
    text = (page.get("text") or "").strip()
    if not text:
        return steps.fail("The page came back with no readable text, so nothing was saved.")

    host = urlsplit(page.get("url") or url).hostname or "page"
    title = " ".join((page.get("title") or "").split()) or host
    folder = dc.expand(dc.text_arg(args, "folder", DEFAULT_FOLDER))
    target = folder / f"{dc.stamp('%Y%m%d-%H%M%S')}-{dc.slug(title, host)}.md"

    lines = [f"# {title}", "", f"Source: {page.get('url') or url}", f"Saved: {dc.stamp()}"]
    note = " ".join(dc.text_arg(args, "note").split())
    if note:
        lines.append(f"Note: {note}")
    if page.get("truncated"):
        lines.append("Only the first part of the page is saved here.")
    content = "\n".join(lines) + "\n\n" + text + "\n"

    result = await steps.run("create_folder", {"path": str(folder)})
    if not result.get("ok"):
        return steps.step_failed("creating the folder", result)
    result = await steps.run("write_new_file", {"path": str(target), "content": content})
    if not result.get("ok"):
        return steps.step_failed("writing the file", result)

    message = f"Saved the text of {title} to {target}."
    if page.get("truncated"):
        message += " The page was long, so only the first part is in the file."
    return steps.done(message, path=str(target), title=title, url=page.get("url") or url, truncated=bool(page.get("truncated")))
