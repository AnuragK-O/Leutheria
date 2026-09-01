import asyncio
import json

import websockets

from agent.core import agent_loop
from agent.core.dispatcher import dispatch
from agent.core.llm_anthropic import AnthropicBackend

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
    Plain text drives the LLM agentic loop (step 4): the model can call
    tools, see their results, and call more tools before giving a final
    answer -- every tool call still runs through the same dispatcher, so
    safety tiering applies either way."""
    if "tool" in payload:
        result = dispatch(payload["tool"], payload.get("args", {}))
        return {"type": "tool_result", "tool": payload["tool"], "result": result}

    text = payload.get("text", "")
    if not text:
        return {"type": "response", "text": ""}

    return await agent_loop.run(get_backend(), text)


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
