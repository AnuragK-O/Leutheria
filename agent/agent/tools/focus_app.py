from agent.core import session, ui_access


def focus_app(name: str) -> dict:
    """Bring an app to the front and make it the session's working context.

    Distinct from open_app, which only launches: this waits until the app is
    actually frontmost, and records it as the focus so later requests can say
    "add a line about X" without re-naming the app. It's also the natural
    place for the permission prompt -- approving it grants the whole app for
    the session, so the typing that follows doesn't ask again.
    """
    try:
        canonical = ui_access.activate(name)
    except ui_access.UIAccessError as e:
        return {"ok": False, "error": str(e)}

    session.set_focus(canonical)
    return {"ok": True, "app": canonical, "message": f"{canonical} is frontmost and ready"}
