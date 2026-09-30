from agent.core import session, ui_access


def describe_ui(app: str = None) -> dict:
    """Read an app's on-screen structure: what fields, buttons and text areas
    exist, which one has keyboard focus, and what they currently contain.

    Read-only, and safe by construction rather than by policy -- password
    fields come back redacted and the never-readable apps are refused in
    ui_access. That's what lets it stay unprompted: it's the look-before-you-
    act step, and gating it would break the same loop that makes the model
    run list_files before open_app.
    """
    target = app or session.current_focus().get("app") or ui_access.frontmost_app()
    if not target:
        return {"ok": False, "error": "no app specified and nothing is frontmost"}

    try:
        described = ui_access.describe(target)
    except ui_access.UIAccessError as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True, **described}
