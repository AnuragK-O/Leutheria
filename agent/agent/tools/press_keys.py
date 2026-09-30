from agent.core import session, ui_access


def press_keys(keys: str) -> dict:
    """Send a keyboard shortcut to the frontmost app.

    The companion to type_text: typing puts characters in a field, this is
    how the agent reaches the commands that have no text equivalent -- a new
    document, save, undo. Without it the model reaches for run_command and
    osascript to do things a keystroke does (observed in testing), which is
    both clumsier and a much wider grant than it needs.
    """
    front = ui_access.frontmost_app()
    if not front:
        return {"ok": False, "error": "nothing is frontmost to send keys to"}

    if not session.has_grant(front):
        return {"ok": False, "error": f"no live permission to control {front}"}

    if ui_access.is_denied(front):
        return {"ok": False, "error": f"{front} can never be controlled"}

    try:
        ui_access.press_keys(keys, expected_app=front)
    except ui_access.UIAccessError as e:
        return {"ok": False, "error": str(e)}

    where = ui_access.focused_context()
    session.set_focus(front)
    return {
        "ok": True,
        "app": front,
        "keys": keys,
        "window": where.get("window"),
        "focused_role": where.get("role"),
        "message": f"pressed {keys} in {front}",
    }
