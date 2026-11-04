from agent.core import session, ui_access


def press_keys(keys: str) -> dict:
    """Send a keyboard shortcut to the frontmost app.

    The companion to type_text: typing puts characters in a field, this is
    how the agent reaches the commands that have no text equivalent -- a new
    document, save, undo. Without it the model reaches for run_command and
    osascript to do things a keystroke does (observed in testing), which is
    both clumsier and a much wider grant than it needs.
    """
    app = ui_access.frontmost()
    front = app.name if app else ""
    if not front:
        return {"ok": False, "error": "nothing is frontmost to send keys to"}

    # Bundle ID first, name as the fallback: a localised name
    # ("Systemeinstellungen") is not on the name list. The grant is keyed the same way
    # the dispatcher keyed it -- by the app, not by how it was spelled.
    if ui_access.is_denied_app(app):
        return {"ok": False, "error": f"{front} can never be controlled"}

    if not session.has_grant(app.key):
        return {"ok": False, "error": f"no live permission to control {front}"}

    try:
        ui_access.press_keys(keys, expected_app=app)
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
