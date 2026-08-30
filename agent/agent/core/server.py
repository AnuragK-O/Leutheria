import asyncio
import json

import websockets

from agent.core.dispatcher import dispatch

HOST = "127.0.0.1"
PORT = 8765


async def handle_message(payload: dict) -> dict:
    """Step 3: a bare "tool" field routes to the manual dispatcher.
    Step 4 replaces the no-tool path with the LLM tool-call loop."""
    if "tool" in payload:
        result = dispatch(payload["tool"], payload.get("args", {}))
        return {"type": "tool_result", "tool": payload["tool"], "result": result}

    text = payload.get("text", "")
    return {"type": "response", "text": f"echo: {text}"}


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
