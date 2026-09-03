async def run(args: dict, websocket, pending: dict) -> dict:
    """Jot down a timestamped note and confirm with a notification.
    Composes two safe tools (append_text, show_notification) -- neither is
    destructive, so this always runs in one shot, no confirmation needed.
    """
    from datetime import datetime

    from agent.core.dispatcher import dispatch

    text = args["text"]
    notes_path = args.get("notes_path", "~/Notes/notes.txt")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"[{timestamp}] {text}"

    steps = []

    append_result = await dispatch("append_text", {"path": notes_path, "text": entry}, websocket, pending)
    steps.append({"tool": "append_text", "args": {"path": notes_path, "text": entry}, "result": append_result})
    if not append_result.get("ok"):
        return {"ok": False, "error": append_result.get("error"), "steps": steps}

    notify_args = {"title": "Note saved", "message": text[:80]}
    notify_result = await dispatch("show_notification", notify_args, websocket, pending)
    steps.append({"tool": "show_notification", "args": notify_args, "result": notify_result})

    return {"ok": True, "message": f"Saved note to {notes_path}", "steps": steps}
