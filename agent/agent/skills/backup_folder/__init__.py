async def run(args: dict, websocket, pending: dict) -> dict:
    """Copy a file or folder to a timestamped backup alongside it.
    copy_file refuses to overwrite an existing destination and never touches
    the source, so this is safe and runs in one shot -- no confirmation
    needed, and the timestamp in the name means it never collides.
    """
    from datetime import datetime
    from pathlib import Path

    from agent.core.dispatcher import dispatch

    source = str(Path(args["path"]).expanduser())
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = f"{source.rstrip('/')}-backup-{timestamp}"

    copy_args = {"source": source, "destination": destination}
    result = await dispatch("copy_file", copy_args, websocket, pending)
    steps = [{"tool": "copy_file", "args": copy_args, "result": result}]
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "steps": steps}

    return {"ok": True, "message": f"Backed up {source} to {destination}", "steps": steps}
