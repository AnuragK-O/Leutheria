import asyncio
import json

from agent.core.dispatcher import dispatch
from agent.core.logging_util import log_event
from agent.skills.registry import anthropic_skill_schemas
from agent.tools.registry import anthropic_tool_schemas

MAX_TURNS = 6  # hard cap so a confused model can't loop forever


async def run(backend, user_text: str, websocket=None, pending: dict = None) -> dict:
    """Drive the Claude <-> tool-dispatch loop for one user message.

    Keeps calling the model and feeding tool results back as long as it
    keeps calling tools, up to MAX_TURNS, then returns whatever text (plus
    the tool call trace) it settled on. websocket/pending are passed through
    to dispatch() so a destructive tool mid-loop can pause for a live
    confirmation round-trip without blocking the connection's read loop.
    """
    messages = [{"role": "user", "content": user_text}]
    tools = anthropic_tool_schemas() + anthropic_skill_schemas()
    loop = asyncio.get_running_loop()
    trace = []

    for _ in range(MAX_TURNS):
        turn = await loop.run_in_executor(None, backend.generate, messages, tools)
        log_event(
            "llm_turn",
            text=turn.text,
            tool_calls=[{"name": c.name, "args": c.args} for c in turn.tool_calls],
        )

        if not turn.tool_calls:
            return {"type": "response", "text": turn.text, "trace": trace}

        messages.append({"role": "assistant", "content": turn.content})

        tool_results = []
        for call in turn.tool_calls:
            result = await dispatch(call.name, call.args, websocket, pending)
            log_event("tool_call", tool=call.name, args=call.args, result=result)
            trace.append({"tool": call.name, "args": call.args, "result": result})
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": json.dumps(result),
                    "is_error": isinstance(result, dict) and result.get("ok") is False,
                }
            )
        messages.append({"role": "user", "content": tool_results})

    return {
        "type": "response",
        "text": "(gave up after too many tool calls without a final answer)",
        "trace": trace,
    }
