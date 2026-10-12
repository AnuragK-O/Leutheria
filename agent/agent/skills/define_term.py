from urllib.parse import quote, quote_plus

from agent.skills import dev_common as dc

MAX_TERM = 100

SOURCES = {
    "dictionary": ("Merriam-Webster", lambda t: "https://www.merriam-webster.com/dictionary/" + quote(t, safe="")),
    "thesaurus": ("the Merriam-Webster thesaurus", lambda t: "https://www.merriam-webster.com/thesaurus/" + quote(t, safe="")),
    "wiktionary": ("Wiktionary", lambda t: "https://en.wiktionary.org/wiki/" + quote(t.replace(" ", "_"), safe="")),
    "wikipedia": ("Wikipedia", lambda t: "https://en.wikipedia.org/wiki/Special:Search?search=" + quote_plus(t)),
}


async def run(args: dict, websocket, pending: dict) -> dict:
    """Open the dictionary (or thesaurus / encyclopedia) page for a term."""
    steps = dc.Steps(websocket, pending)
    term = " ".join(dc.text_arg(args, "term").split())[:MAX_TERM]
    if not term:
        return steps.fail("I need a word or phrase to look up.")
    source = dc.text_arg(args, "source", "dictionary").lower()
    if source not in SOURCES:
        return steps.fail(f"I can look a term up in: {', '.join(SOURCES)}.")

    site, build = SOURCES[source]
    url = build(term)
    result = await steps.run("open_url", {"url": url})
    if not result.get("ok"):
        return steps.step_failed("opening the browser", result)
    return steps.done(f"Opened {site} for {term}.", url=url)
