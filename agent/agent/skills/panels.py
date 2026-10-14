import json

from agent.core.logging_util import log_event

# The overlay panels the Electron app can draw (plans/overlay-panels-spec.md).
# show_panel / hide_panel are UI-only: no tool steps, nothing on the machine
# changes, so there's nothing for dispatch() to gate. They send one "panel"
# message on the request's websocket -- for a UI command that's the primary's
# _MirroringSocket, which does not mirror "panel" (the Qt overlay draws no
# panels). A voice command with no window connected gets websocket None.
PANELS = ("chat", "skills", "activity", "permissions", "timers", "voice")

# hide_panel with no panel named hides every open panel.
ALL = "all"

NOT_OPEN = "The Leutheria app isn't open, so there's nowhere to show that. Answer in words instead."


async def _send(websocket, message: dict) -> dict | None:
    """Send the panel message; an error dict if there's no UI to send it to."""
    if websocket is None:
        return {"ok": False, "error": NOT_OPEN, "steps": []}
    try:
        await websocket.send(json.dumps(message))
    except Exception:  # the window closed while the command was running
        return {"ok": False, "error": NOT_OPEN, "steps": []}
    return None


def _unknown(panel) -> dict:
    log_event("panel_unknown", panel=panel)
    return {
        "ok": False,
        "error": f"There's no panel called {panel!r}. The panels are: {', '.join(PANELS)}.",
        "steps": [],
    }


async def show(args: dict, websocket, pending: dict) -> dict:
    panel = args.get("panel")
    if panel not in PANELS:
        return _unknown(panel)
    message = {"type": "panel", "action": "show", "panel": panel}
    query = args.get("query")
    if isinstance(query, str) and query.strip():
        message["query"] = query.strip()
    error = await _send(websocket, message)
    if error:
        return error
    return {"ok": True, "panel": panel, "message": f"Showing the {panel} panel.", "steps": []}


async def hide(args: dict, websocket, pending: dict) -> dict:
    panel = args.get("panel") or ALL
    if panel != ALL and panel not in PANELS:
        return _unknown(panel)
    error = await _send(websocket, {"type": "panel", "action": "hide", "panel": panel})
    if error:
        return error
    what = "all panels" if panel == ALL else f"the {panel} panel"
    return {"ok": True, "panel": panel, "message": f"Hid {what}.", "steps": []}
