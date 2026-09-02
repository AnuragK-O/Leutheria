def _substitute(value, args: dict):
    if isinstance(value, str):
        try:
            return value.format(**args)
        except (KeyError, IndexError):
            return value
    if isinstance(value, dict):
        return {k: _substitute(v, args) for k, v in value.items()}
    return value


async def run_template(definition: dict, args: dict, websocket, pending: dict) -> dict:
    """Execute a generated skill: a plain ordered list of {tool, args} steps
    with {param} placeholders. This is the ONE reviewed interpreter every
    auto-generated skill runs through -- a generated skill is data, not new
    code, so it's structurally incapable of doing anything a hand-written
    skill couldn't. Each step still goes through dispatch(), so a destructive
    step still pauses for its own confirmation, same as always.
    """
    from agent.core.dispatcher import dispatch

    steps_log = []
    for step in definition["steps"]:
        step_args = _substitute(step["args"], args)
        result = await dispatch(step["tool"], step_args, websocket, pending)
        steps_log.append({"tool": step["tool"], "args": step_args, "result": result})
        if not (isinstance(result, dict) and result.get("ok")):
            return {
                "ok": False,
                "error": f"step '{step['tool']}' failed or was declined",
                "steps": steps_log,
            }

    return {
        "ok": True,
        "message": f"Ran generated skill '{definition['name']}' successfully.",
        "steps": steps_log,
    }
