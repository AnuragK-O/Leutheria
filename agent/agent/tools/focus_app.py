from agent.core import session, ui_access


def focus_app(name: str) -> dict:
    """Bring an app to the front and make it the session's working context.

    Distinct from open_app, which only launches: this waits until the app is
    actually frontmost, and records it as the focus so later requests can say
    "add a line about X" without re-naming the app. It's also the natural
    place for the permission prompt -- approving it grants the whole app for
    the session, so the typing that follows doesn't ask again.

    The dispatcher resolved `name` and checked the grant for that app (its
    bundle ID) a moment ago. It is resolved again here and the grant checked
    once more before anything is launched or raised: across the prompt the
    running apps can change, and a different app must not ride on the grant.
    """
    try:
        target = ui_access.target_app(name)
        if not session.has_grant(target.key):
            return {"ok": False, "error": f"no live permission to control {target.name}"}
        running = ui_access.activate(target)
    except ui_access.UIAccessError as e:
        return {"ok": False, "error": str(e)}

    session.set_focus(running.name)
    return {"ok": True, "app": running.name, "message": f"{running.name} is frontmost and ready"}
