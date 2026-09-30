from agent.core import session, ui_access


def type_text(text: str) -> dict:
    """Type into whatever currently has keyboard focus.

    Targets the frontmost app rather than taking an app argument, which is
    deliberate: the dispatcher checks the grant against what's actually in
    front at the moment of typing, so if the user switches to an ungranted
    app mid-session the text can't follow them there.
    """
    front = ui_access.frontmost_app()
    if not front:
        return {"ok": False, "error": "nothing is frontmost to type into"}

    # The dispatcher already checked this a moment ago; checking again here is
    # the same belt-and-braces as dispatch() re-checking preferences, and it
    # closes the gap between that check and the first keystroke.
    if not session.has_grant(front):
        return {"ok": False, "error": f"no live permission to control {front}"}

    if ui_access.is_denied(front):
        return {"ok": False, "error": f"{front} can never be typed into"}

    where = ui_access.focused_context()

    try:
        outcome = ui_access.type_text(text, expected_app=front)
    except ui_access.UIAccessError as e:
        return {"ok": False, "error": str(e)}

    session.set_focus(front)

    if not outcome["complete"]:
        # Partial, because focus moved. Reported as a failure so the model
        # tells the user rather than assuming the whole note landed.
        return {
            "ok": False,
            "app": front,
            "characters": outcome["typed"],
            "error": (
                f"focus left {front} after {outcome['typed']} of {len(text)} characters; "
                "typing stopped there"
            ),
        }

    result = {
        "ok": True,
        "app": front,
        "characters": outcome["typed"],
        "window": where.get("window"),
        "focused_role": where.get("role"),
        "message": f"typed into {front}",
    }

    # A dialog or a non-text control almost certainly isn't the document the
    # user had in mind. Still a success -- the keystrokes did land -- but say
    # so plainly, so the model can check with describe_ui and correct instead
    # of reporting that the note was saved.
    if where.get("dialog") or where.get("role") not in ("AXTextArea", "AXTextField", None):
        result["warning"] = (
            f"focus was on a {where.get('role') or 'unknown'} control"
            + (f" in the dialog '{where['window']}'" if where.get("dialog") and where.get("window") else "")
            + " -- this may not be the document the user meant; check with describe_ui"
        )
    return result
