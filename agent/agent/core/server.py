import asyncio
import json

import websockets

from agent.core.dispatcher import dispatch
from agent.core.llm import TextResponse, ToolCall
from agent.core.llm_anthropic import AnthropicBackend
from agent.tools.registry import anthropic_tool_schemas

HOST = "127.0.0.1"
PORT = 8765

_backend = None


def get_backend():
    global _backend
    if _backend is None:
        _backend = AnthropicBackend()
    return _backend


async def handle_message(payload: dict) -> dict:
    """A bare "tool" field routes straight to the manual dispatcher (step 3).
    Plain text goes through the LLM tool-call path (step 4): the model picks
    a tool (or just replies), and a tool pick still runs through the same
    dispatcher, so safety tiering applies either way."""
    if "tool" in payload:
        result = dispatch(payload["tool"], payload.get("args", {}))
        return {"type": "tool_result", "tool": payload["tool"], "result": result}

    text = payload.get("text", "")
    if not text:
        return {"type": "response", "text": ""}

    loop = asyncio.get_running_loop()
    decision = await loop.run_in_executor(
        None,
        get_backend().generate,
        [{"role": "user", "content": text}],
        anthropic_tool_schemas(),
    )

    if isinstance(decision, ToolCall):
        result = dispatch(decision.name, decision.args)
        return {"type": "tool_result", "tool": decision.name, "result": result}

    assert isinstance(decision, TextResponse)
    return {"type": "response", "text": decision.text}


async def handler(websocket):
    async for raw in websocket:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.send(json.dumps({"type": "error", "text": "invalid JSON"}))
            continue

        response = await handle_message(payload)
        await websocket.send(json.dumps(response))


async def main():
    async with websockets.serve(handler, HOST, PORT):
        print(f"AGENT_READY ws://{HOST}:{PORT}", flush=True)
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
