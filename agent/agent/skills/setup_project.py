async def run(args: dict, websocket, pending: dict) -> dict:
    """Create a project folder, git init it, and open it in VS Code.

    Imports dispatch() lazily to avoid a circular import: dispatcher imports
    the skills registry, and this skill needs to call back into dispatch()
    for its destructive sub-steps (so git init / opening the editor still go
    through the normal confirmation flow, one prompt per sub-step).
    """
    from pathlib import Path

    from agent.core.dispatcher import dispatch
    from agent.tools.create_folder import create_folder

    name = args["name"]
    base_dir = args.get("base_dir", "~/Projects")
    # Expand ~ before building shell strings -- /bin/sh's cd doesn't expand
    # a quoted "~" the way an interactive shell would, so a raw "~/..."
    # path here would silently fail the cd inside git_cmd below.
    path = str(Path(base_dir).expanduser() / name)

    steps = []

    folder_result = create_folder(path)
    steps.append({"tool": "create_folder", "args": {"path": path}, "result": folder_result})
    if not folder_result.get("ok"):
        return {"ok": False, "error": folder_result.get("error"), "steps": steps}

    git_cmd = f'cd "{path}" && git init'
    git_result = await dispatch("run_command", {"cmd": git_cmd}, websocket, pending)
    steps.append({"tool": "run_command", "args": {"cmd": git_cmd}, "result": git_result})
    if not git_result.get("ok"):
        return {"ok": False, "error": "git init step failed or was declined", "steps": steps}

    open_cmd = f'open -a "Visual Studio Code" "{path}"'
    open_result = await dispatch("run_command", {"cmd": open_cmd}, websocket, pending)
    steps.append({"tool": "run_command", "args": {"cmd": open_cmd}, "result": open_result})
    if not open_result.get("ok"):
        return {"ok": False, "error": "opening VS Code step failed or was declined", "steps": steps}

    return {
        "ok": True,
        "message": f"Project '{name}' created at {path}, git initialized, and opened in VS Code.",
        "steps": steps,
    }
