async def run(args: dict, websocket, pending: dict) -> dict:
    """Create a project folder, git init it, and open it in VS Code.

    Every step goes through dispatch() -- create_folder included -- so a
    capability the user switched off, or asked to confirm, is honoured here
    exactly as it would be for a direct call, and git init / opening the
    editor still pause for their own confirmation, one prompt per step.

    Imports are lazy to avoid a circular import: dispatcher imports the
    skills registry, which imports this module.
    """
    from agent.core.dispatcher import dispatch
    from agent.skills import dev_common

    steps = []

    async def step(tool: str, step_args: dict) -> dict:
        result = await dispatch(tool, step_args, websocket, pending)
        if not isinstance(result, dict):
            result = {"ok": False, "error": f"{tool} returned no result"}
        steps.append({"tool": tool, "args": step_args, "result": result})
        return result

    # The same rule the scaffold skills use: a plain folder name, not a path
    # and nothing that reads as shell syntax. base_dir has ~ expanded here,
    # before any shell string is built -- /bin/sh's cd doesn't expand a
    # quoted "~", which made git init fail silently once (BUGS.md #5).
    root, name, error = dev_common.project_root(args)
    if error:
        return {"ok": False, "error": error, "steps": steps}
    path = str(root)

    folder_result = await step("create_folder", {"path": path})
    if not folder_result.get("ok"):
        return {"ok": False, "error": folder_result.get("error"), "steps": steps}

    # The name is validated, but base_dir is free text, and both end up in a
    # shell string: every interpolated value is quoted, always.
    git_result = await step("run_command", {"cmd": f"cd {dev_common.q(path)} && git init"})
    if not git_result.get("ok"):
        return {"ok": False, "error": "git init step failed or was declined", "steps": steps}

    open_result = await step("run_command", {"cmd": f'open -a "Visual Studio Code" {dev_common.q(path)}'})
    if not open_result.get("ok"):
        return {"ok": False, "error": "opening VS Code step failed or was declined", "steps": steps}

    return {
        "ok": True,
        "message": f"Project '{name}' created at {path}, git initialized, and opened in VS Code.",
        "steps": steps,
    }
