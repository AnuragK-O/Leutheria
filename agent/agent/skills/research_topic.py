from agent.skills import dev_common as dc
from agent.skills.web_search import ENGINES, MAX_QUERY, search_url

# A research request opens a small fixed set of tabs, never one per idea:
# MAX_TABS is the cap whatever is asked for.
MAX_TABS = 4
SOURCES = {
    "web": "google",
    "wikipedia": "wikipedia",
    "news": "news",
    "scholar": "scholar",
    "videos": "youtube",
    "discussion": "reddit",
}
DEFAULT_SOURCES = ("web", "wikipedia", "news")


async def run(args: dict, websocket, pending: dict) -> dict:
    """Open a handful of starting points for a topic, one browser tab each,
    and say which were opened."""
    steps = dc.Steps(websocket, pending)
    topic = dc.text_arg(args, "topic")[:MAX_QUERY]
    if not topic:
        return steps.fail("I need a topic to research.")

    requested = args.get("sources")
    if not isinstance(requested, list) or not requested:
        requested = list(DEFAULT_SOURCES)
    sources, unknown = [], []
    for item in requested:
        key = item.strip().lower() if isinstance(item, str) else ""
        if key in SOURCES:
            if key not in sources:
                sources.append(key)
        else:
            unknown.append(str(item)[:40])
    if not sources:
        return steps.fail(f"None of those sources are ones I know. Known ones: {', '.join(SOURCES)}.")
    dropped = sources[MAX_TABS:]
    sources = sources[:MAX_TABS]

    opened = []
    for source in sources:
        engine = SOURCES[source]
        url = search_url(engine, topic)
        result = await steps.run("open_url", {"url": url})
        if not result.get("ok"):
            return steps.step_failed(f"opening {ENGINES[engine][0]}", result, opened=opened)
        opened.append({"source": source, "site": ENGINES[engine][0], "url": url})

    sites = [o["site"] for o in opened]
    spoken = sites[0] if len(sites) == 1 else ", ".join(sites[:-1]) + " and " + sites[-1]
    message = f"Opened {len(opened)} tabs on {topic}: {spoken}."
    if dropped or unknown:
        message += f" Skipped {', '.join(dropped + unknown)}."
    return steps.done(message, opened=opened)
