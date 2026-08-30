from agent.tools.registry import TOOLS


def dispatch(tool_name: str, args: dict) -> dict:
    tool = TOOLS.get(tool_name)
    if tool is None:
        return {"ok": False, "error": f"unknown tool: {tool_name}"}

    if tool["safety"] == "destructive":
        # Real confirmation UX lands in roadmap step 5. For now, block it
        # outright so a destructive tool can never fire from a bare command.
        return {"ok": False, "error": f"{tool_name} is destructive and requires confirmation (not yet implemented)"}

    try:
        return tool["fn"](**args)
    except TypeError as e:
        return {"ok": False, "error": str(e)}
