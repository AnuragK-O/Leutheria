import asyncio
import json
import uuid

from agent.core import trust
from agent.core.logging_util import log_event
from agent.core.tts import speak
from agent.skills.registry import SKILLS
from agent.tools.registry import TOOLS

CONFIRMATION_TIMEOUT = 120  # seconds -- an unanswered prompt is treated as declined


async def dispatch(
    tool_name: str, args: dict, websocket=None, pending: dict = None, intro_text: str = ""
) -> dict:
    if tool_name in SKILLS:
        return await SKILLS[tool_name]["fn"](args, websocket, pending)

    tool = TOOLS.get(tool_name)
    if tool is None:
        return {"ok": False, "error": f"unknown tool: {tool_name}"}

    if tool["safety"] == "destructive" and not trust.is_trusted(tool_name, args):
        approved, remember = await _confirm(tool_name, args, websocket, pending, intro_text)

        if remember == "session":
            trust.trust_for_session(tool_name, args)
        elif remember == "always":
            trust.trust_always(tool_name, args)

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


async def _confirm(tool_name: str, args: dict, websocket, pending: dict, intro_text: str = "") -> tuple:
    if websocket is None or pending is None:
        # No interactive channel to confirm through (e.g. dispatch called
        # directly outside a live connection) -- fail closed, not open.
        return False, None

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

    # Speak the prompt so this can be answered by voice alone. Prefer the
    # model's own accompanying explanation for this turn (SYSTEM_PROMPT.md
    # already asks it to briefly explain a destructive action before taking
    # it) over a generic fallback -- it's more natural and specific.
    spoken = intro_text.strip() or f"I want to run {tool_name}. Should I go ahead?"
    await speak(websocket, spoken)

    try:
        result = await asyncio.wait_for(future, timeout=CONFIRMATION_TIMEOUT)
        return bool(result.get("approved")), result.get("remember")
    except asyncio.TimeoutError:
        return False, None
    finally:
        pending.pop(confirmation_id, None)
