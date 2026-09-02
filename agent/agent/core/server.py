import asyncio
import base64
import json
import uuid

import websockets

from agent.core import agent_loop, skill_learning, tts
from agent.core.audio_input import transcribe_payload
from agent.core.dispatcher import dispatch
from agent.core.llm_anthropic import AnthropicBackend
from agent.core.logging_util import log_event
from agent.skills.registry import register_skill

HOST = "127.0.0.1"
PORT = 8765
# websockets' default max_size (1 MiB) is easily exceeded by a "speech"
# message's base64 WAV audio for anything but a very short reply -- e.g. a
# ~500-character reply's synthesized audio hit ~1.2 MB and silently killed
# the connection (found via testing -- see BUGS.md). Recorded audio clips
# for STT could also approach this for a long push-to-talk recording.
MAX_MESSAGE_SIZE = 20 * 1024 * 1024  # 20 MiB

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


async def process_command(websocket, payload: dict, pending: dict, pending_skills: dict) -> None:
    response = await handle_message(payload, websocket, pending)

    # Must be checked against history *before* this request logs its own
    # message_out below -- otherwise this request's own entry is already on
    # disk by the time the check runs and trivially matches itself, firing
    # a proposal every time instead of only when actually warranted.
    # `check` is NOT_APPLICABLE (skip), a past trace (confident repeat), or
    # None (first occurrence -- still propose, but with clarifying
    # questions instead of a confident diff). (Found via testing -- BUGS.md.)
    check = skill_learning.check_before_logging(response)
    should_propose = check is not skill_learning.NOT_APPLICABLE
    past_trace = check if should_propose else None

    log_event("message_out", response=response)
    await websocket.send(json.dumps(response))

    if response.get("type") == "response" and response.get("text"):
        await speak(websocket, response["text"])

    if should_propose:
        user_text = payload.get("text", "")
        await propose_skill(websocket, response["trace"], past_trace, user_text, pending_skills)


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


async def propose_skill(websocket, trace: list, past_trace, user_text: str, pending_skills: dict) -> None:
    """Generate and send a skill proposal. If past_trace is given, this is a
    confirmed repeat -- diff the two concrete runs for a confident
    definition. Otherwise this is the first time the sequence has been
    seen -- generate from one example, flagging anything uncertain as a
    clarifying question rather than guessing. Entirely non-blocking: this
    never delays or affects the response that was already sent, same
    principle as speak() above.
    """
    loop = asyncio.get_running_loop()
    if past_trace is not None:
        definition = await loop.run_in_executor(
            None, skill_learning.generate_skill_definition, get_backend(), trace, past_trace
        )
    else:
        definition = await loop.run_in_executor(
            None, skill_learning.generate_skill_definition_single, get_backend(), trace, user_text
        )
    if definition is None:
        return

    proposal_id = str(uuid.uuid4())
    pending_skills[proposal_id] = definition
    await websocket.send(
        json.dumps(
            {
                "type": "skill_proposed",
                "id": proposal_id,
                "name": definition["name"],
                "description": definition["description"],
                "steps": definition["steps"],
                "uncertain_params": definition.get("uncertain_params", []),
            }
        )
    )
    log_event(
        "skill_proposed",
        id=proposal_id,
        name=definition["name"],
        confident=past_trace is not None,
        uncertain_count=len(definition.get("uncertain_params", [])),
    )


async def handler(websocket):
    # Per-connection state. `pending` maps a confirmation id to the Future its
    # resolution unblocks; `pending_skills` maps a proposal id to the skill
    # definition awaiting an approve/decline. Commands are dispatched as
    # background tasks (not awaited inline) specifically so this loop stays
    # free to read an incoming "confirm"/"skill_response" while a command is
    # paused mid-flight waiting on one.
    pending: dict = {}
    pending_skills: dict = {}

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

        if payload.get("type") == "skill_response":
            definition = pending_skills.pop(payload.get("id"), None)
            if definition and payload.get("approved"):
                finalized = skill_learning.finalize_definition(definition, payload.get("resolutions", {}))
                register_skill(finalized)
                log_event("skill_registered", name=finalized["name"])
                await websocket.send(json.dumps({"type": "skill_saved", "name": finalized["name"]}))
            elif definition:
                log_event("skill_declined", name=definition["name"])
            continue

        log_event("message_in", payload=payload)
        asyncio.create_task(process_command(websocket, payload, pending, pending_skills))


async def main():
    async with websockets.serve(handler, HOST, PORT, max_size=MAX_MESSAGE_SIZE):
        print(f"AGENT_READY ws://{HOST}:{PORT}", flush=True)
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
