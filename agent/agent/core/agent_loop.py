import asyncio
import json

from agent.core.dispatcher import dispatch
from agent.tools.registry import anthropic_tool_schemas

MAX_TURNS = 6  # hard cap so a confused model can't loop forever


async def run(backend, user_text: str) -> dict:
    """Drive the Claude <-> tool-dispatch loop for one user message.

    Keeps calling the model and feeding tool results back as long as it
    keeps calling tools, up to MAX_TURNS, then returns whatever text (plus
    the tool call trace) it settled on.
    """
    messages = [{"role": "user", "content": user_text}]
    tools = anthropic_tool_schemas()
    loop = asyncio.get_running_loop()
    trace = []

    for _ in range(MAX_TURNS):
        turn = await loop.run_in_executor(None, backend.generate, messages, tools)

        if not turn.tool_calls:
            return {"type": "response", "text": turn.text, "trace": trace}

        messages.append({"role": "assistant", "content": turn.content})

        tool_results = []
        for call in turn.tool_calls:
            result = dispatch(call.name, call.args)
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
