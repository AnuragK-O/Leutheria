import asyncio
import json
import uuid

from agent.core.logging_util import log_event
from agent.tools.registry import TOOLS

CONFIRMATION_TIMEOUT = 120  # seconds -- an unanswered prompt is treated as declined


async def dispatch(tool_name: str, args: dict, websocket=None, pending: dict = None) -> dict:
    tool = TOOLS.get(tool_name)
    if tool is None:
        return {"ok": False, "error": f"unknown tool: {tool_name}"}

    if tool["safety"] == "destructive":
        approved = await _confirm(tool_name, args, websocket, pending)
        if not approved:
            log_event("tool_declined", tool=tool_name, args=args)
            return {
                "ok": False,
                "error": f"{tool_name} was declined by the user (or the confirmation prompt timed out)",
            }

    try:
        return tool["fn"](**args)
    except TypeError as e:
        return {"ok": False, "error": str(e)}


async def _confirm(tool_name: str, args: dict, websocket, pending: dict) -> bool:
    if websocket is None or pending is None:
        # No interactive channel to confirm through (e.g. dispatch called
        # directly outside a live connection) -- fail closed, not open.
        return False

    confirmation_id = str(uuid.uuid4())
    future = asyncio.get_running_loop().create_future()
    pending[confirmation_id] = future

    await websocket.send(
        json.dumps(
            {
                "type": "confirmation_required",
                "id": confirmation_id,
                "tool": tool_name,
                "args": args,
            }
        )
    )
    log_event("confirmation_requested", id=confirmation_id, tool=tool_name, args=args)

    try:
        return await asyncio.wait_for(future, timeout=CONFIRMATION_TIMEOUT)
    except asyncio.TimeoutError:
        return False
    finally:
        pending.pop(confirmation_id, None)
