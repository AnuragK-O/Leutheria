from urllib.parse import quote_plus

from agent.skills import dev_common as dc

MAX_QUERY = 300

# name -> (spoken name, URL prefix the encoded query is appended to)
ENGINES = {
    "google": ("Google", "https://www.google.com/search?q="),
    "duckduckgo": ("DuckDuckGo", "https://duckduckgo.com/?q="),
    "bing": ("Bing", "https://www.bing.com/search?q="),
    "youtube": ("YouTube", "https://www.youtube.com/results?search_query="),
    "wikipedia": ("Wikipedia", "https://en.wikipedia.org/wiki/Special:Search?search="),
    "maps": ("Google Maps", "https://www.google.com/maps/search/?api=1&query="),
    "images": ("Google Images", "https://www.google.com/search?tbm=isch&q="),
    "news": ("Google News", "https://news.google.com/search?q="),
    "scholar": ("Google Scholar", "https://scholar.google.com/scholar?q="),
    "reddit": ("Reddit", "https://www.reddit.com/search/?q="),
    "stackoverflow": ("Stack Overflow", "https://stackoverflow.com/search?q="),
    "github": ("GitHub", "https://github.com/search?q="),
}
DEFAULT_ENGINE = "google"


def search_url(engine: str, query: str) -> str:
    return ENGINES[engine][1] + quote_plus(query)


async def run(args: dict, websocket, pending: dict) -> dict:
    """Open a search results page in the browser. With no query, searches
    for whatever text is on the clipboard."""
    steps = dc.Steps(websocket, pending)
    engine = dc.text_arg(args, "engine", DEFAULT_ENGINE).lower().replace(" ", "")
    if engine not in ENGINES:
        return steps.fail(f"I don't know the search site {engine}. Known ones: {', '.join(ENGINES)}.")

    query = dc.text_arg(args, "query")
    from_clipboard = False
    if not query:
        clip = await steps.run("get_clipboard", {})
        if not clip.get("ok"):
            return steps.step_failed("reading the clipboard", clip)
        query = " ".join((clip.get("text") or "").split())
        from_clipboard = True
        if not query:
            return steps.fail("There is nothing to search for: no query was given and the clipboard is empty.")
    query = query[:MAX_QUERY]

    url = search_url(engine, query)
    result = await steps.run("open_url", {"url": url})
    if not result.get("ok"):
        return steps.step_failed("opening the browser", result)
    shown = query if len(query) <= 60 else query[:57] + "..."
    source = " from the clipboard" if from_clipboard else ""
    return steps.done(f"Opened a {ENGINES[engine][0]} search for {shown}{source}.", url=url, query=query)
