import asyncio
import base64
import json

import websockets

from agent.core import agent_loop, tts
from agent.core.audio_input import transcribe_payload
from agent.core.dispatcher import dispatch
from agent.core.llm_anthropic import AnthropicBackend
from agent.core.logging_util import log_event

HOST = "127.0.0.1"
PORT = 8765

_backend = None


def get_backend():
    global _backend
    if _backend is None:
        _backend = AnthropicBackend()
    return _backend


async def handle_message(payload: dict, websocket, pending: dict) -> dict:
    """A bare "tool" field routes straight to the manual dispatcher (step 3).
    Plain text drives the LLM agentic loop (step 4): the model can call
    tools, see their results, and call more tools before giving a final
    answer -- every tool call still runs through the same dispatcher, so
    safety tiering applies either way. A destructive tool pauses mid-flight
    for a live yes/no over the same connection (step 5) -- see dispatch()."""
    if "tool" in payload:
        result = await dispatch(payload["tool"], payload.get("args", {}), websocket, pending)
        log_event("tool_call", tool=payload["tool"], args=payload.get("args", {}), result=result)
        return {"type": "tool_result", "tool": payload["tool"], "result": result}

    if payload.get("type") == "audio":
        text = await transcribe_payload(payload)
        log_event("stt_transcript", text=text)
        await websocket.send(json.dumps({"type": "transcript", "text": text}))
        if not text:
            return {"type": "response", "text": "(didn't catch that -- heard nothing)"}
        return await agent_loop.run(get_backend(), text, websocket, pending)

    text = payload.get("text", "")
    if not text:
        return {"type": "response", "text": ""}

    return await agent_loop.run(get_backend(), text, websocket, pending)


async def process_command(websocket, payload: dict, pending: dict) -> None:
    response = await handle_message(payload, websocket, pending)
    log_event("message_out", response=response)
    await websocket.send(json.dumps(response))

    if response.get("type") == "response" and response.get("text"):
        await speak(websocket, response["text"])


async def speak(websocket, text: str) -> None:
    """Synthesize the final reply locally with Piper and send it as a
    separate message so a TTS failure never blocks the actual response."""
    try:
        loop = asyncio.get_running_loop()
        audio_bytes = await loop.run_in_executor(None, tts.synthesize, text)
        await websocket.send(
            json.dumps({"type": "speech", "data": base64.b64encode(audio_bytes).decode()})
        )
    except Exception as e:
        log_event("tts_error", error=str(e))


async def handler(websocket):
    # Per-connection: maps a confirmation id to the Future its resolution
    # unblocks. Commands are dispatched as background tasks (not awaited
    # inline) specifically so this loop stays free to read an incoming
    # "confirm" reply while a command is paused mid-flight waiting on one.
    pending: dict = {}

    async for raw in websocket:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.send(json.dumps({"type": "error", "text": "invalid JSON"}))
            continue

        if payload.get("type") == "confirm":
            future = pending.get(payload.get("id"))
            if future and not future.done():
                future.set_result(bool(payload.get("approved")))
            continue

        log_event("message_in", payload=payload)
        asyncio.create_task(process_command(websocket, payload, pending))


async def main():
    async with websockets.serve(handler, HOST, PORT):
        print(f"AGENT_READY ws://{HOST}:{PORT}", flush=True)
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
