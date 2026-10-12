import html
import json

from agent.skills import dev_common as dc

CSS = """\
:root {
  color-scheme: light dark;
  font-family: system-ui, -apple-system, sans-serif;
}

body {
  max-width: 40rem;
  margin: 4rem auto;
  padding: 0 1rem;
  line-height: 1.5;
}
"""


def files_for(title: str) -> dict:
    # The title is free text, so it is escaped for each place it lands:
    # HTML text here, a JSON string literal in the script.
    safe = html.escape(title, quote=True)
    return {
        "index.html": (
            "<!DOCTYPE html>\n"
            '<html lang="en">\n'
            "<head>\n"
            '  <meta charset="utf-8">\n'
            '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"  <title>{safe}</title>\n"
            '  <link rel="stylesheet" href="styles.css">\n'
            "</head>\n"
            "<body>\n"
            f"  <h1>{safe}</h1>\n"
            '  <p id="status">Loading.</p>\n'
            '  <script src="script.js"></script>\n'
            "</body>\n"
            "</html>\n"
        ),
        "styles.css": CSS,
        "script.js": (
            f"const title = {json.dumps(title)};\n"
            'document.getElementById("status").textContent = `${title} is ready.`;\n'
        ),
    }


async def run(args: dict, websocket, pending: dict) -> dict:
    """Scaffold a static web page (index.html, styles.css, script.js) in a
    new folder. Everything here is a safe tool, so nothing asks."""
    steps = dc.Steps(websocket, pending)
    root, name, error = dc.project_root(args)
    if error:
        return steps.fail(error)
    title = dc.text_arg(args, "title", name)[:200]

    failure = await dc.scaffold(steps, root, files_for(title))
    if failure:
        return failure

    extras = []
    if dc.flag(args, "open_in_browser"):
        index = root / "index.html"
        result = await steps.run("open_url", {"url": index.resolve().as_uri()})
        if not result.get("ok"):
            return steps.step_failed("opening the page in the browser", result, created=str(root))
        extras.append("opened in the browser")
    app = dc.text_arg(args, "open_in")
    if app:
        failure = await dc.open_in_app(steps, root, app, root)
        if failure:
            return failure
        extras.append(f"opened in {app}")

    message = f"Web page {name} created at {root} with index.html, styles.css and script.js"
    message += (", " + ", ".join(extras) if extras else "") + "."
    return steps.done(message, path=str(root))
