import asyncio
import json
import os
import uuid

import websockets
from websockets.exceptions import ConnectionClosed

from agent.core import agent_loop, audio_io, control, skill_learning, voice_session, voice_settings
from agent.core.audio_input import transcribe_payload
from agent.core.dispatcher import dispatch
from agent.core.llm_anthropic import AnthropicBackend
from agent.core.logging_util import log_event
from agent.core.tts import speak
from agent.core.voice_confirm import interpret_yes_no
from agent.skills.registry import register_skill

HOST = "127.0.0.1"
# Overridable so a test server can run beside the real app without fighting
# it for the port (see scripts/voice_server_check.py).
PORT = int(os.environ.get("LEUTHERIA_PORT", "8765"))
# websockets' default max_size (1 MiB) was easily exceeded by the old
# base64 "speech" message (BUGS.md #7). Replies are played Python-side now,
# but push-to-talk still uploads whole recorded clips as base64, and a long
# recording can approach the default too.
MAX_MESSAGE_SIZE = 20 * 1024 * 1024  # 20 MiB

_backend = None

# Every open UI connection, oldest first, as (websocket, pending,
# pending_skills). Confirmations and skill proposals are per-connection, but
# the voice session isn't tied to any connection -- so voice commands run on
# the most recently opened one (the "primary"). With nothing connected they
# run with websocket=None, which makes any destructive step fail closed in
# dispatcher._confirm(); that's the intended behaviour, not a gap.
_connections: list = []

# Fire-and-forget tasks are kept referenced until done -- asyncio only holds
# weak references, so an unreferenced task can be collected mid-flight.
_background: set = set()


def get_backend():
    global _backend
    if _backend is None:
        _backend = AnthropicBackend()
    return _backend


def _primary() -> tuple:
    if _connections:
        return _connections[-1]
    return None, {}, {}


def _in_background(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _send(websocket, message: dict) -> None:
    """Send, tolerating a missing or already-closed connection -- a voice
    command can outlive the window it was reported to, and nothing about the
    command itself depends on the UI hearing about it."""
    if websocket is None:
        return
    try:
        await websocket.send(json.dumps(message))
    except ConnectionClosed:
        pass


async def broadcast(message: dict) -> None:
    """Voice status (state, levels, session start/end) goes to every open
    connection: it's status, not conversation, and more than one listener
    (the app plus a debugging client) should all see the same thing."""
    for websocket, _, _ in list(_connections):
        await _send(websocket, message)


async def _resolve_confirmation_by_voice(text: str, websocket, pending: dict) -> bool:
    """If a confirmation is waiting on this connection, a clear yes/no
    answer resolves it directly instead of being treated as a brand-new
    command -- this is what makes voice-only approval possible (the prompt
    itself is spoken by _confirm() in dispatcher.py). An ambiguous reply
    returns False, leaving the confirmation open."""
    if not pending:
        return False
    resolution = interpret_yes_no(text)
    if resolution is None:
        return False
    confirmation_id, future = next(reversed(pending.items()))
    if future.done():
        return False
    future.set_result({"approved": resolution, "remember": None})
    log_event("confirmation_answered_by_voice", id=confirmation_id, approved=resolution)
    # Tell the UI too, so the on-screen card settles into its answered state
    # instead of sitting there with live buttons for a question that's
    # already been answered.
    await _send(
        websocket,
        {"type": "confirmation_resolved", "id": confirmation_id, "approved": resolution, "by": "voice"},
    )
    return True


async def handle_transcript(text: str, websocket, pending: dict, origin: str) -> dict:
    """The one path every spoken input takes, push-to-talk or live session:
    show the user what was heard, let it answer a pending confirmation, and
    otherwise run it through the agent loop exactly like typed text."""
    await _send(websocket, {"type": "transcript", "text": text, "origin": origin})
    if await _resolve_confirmation_by_voice(text, websocket, pending):
        return {"type": "response", "text": ""}
    if not text:
        return {"type": "response", "text": "(didn't catch that -- heard nothing)"}
    return await agent_loop.run(get_backend(), text, websocket, pending)


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
        # Recorded on the payload so a skill proposal generated from this
        # request knows what was asked, same as for a typed command.
        payload["text"] = text
        return await handle_transcript(text, websocket, pending, "push_to_talk")

    text = payload.get("text", "")
    if not text:
        return {"type": "response", "text": ""}

    return await agent_loop.run(get_backend(), text, websocket, pending)


async def process_command(
    websocket, payload: dict, pending: dict, pending_skills: dict, origin: str = None
) -> None:
    if origin == "voice_session":
        response = await handle_transcript(payload["text"], websocket, pending, origin)
        # Tagged so main.js shows the reply even though the renderer never
        # sent a request for it.
        response["origin"] = origin
    else:
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
    await _send(websocket, response)

    if response.get("type") == "response" and response.get("text"):
        await speak(websocket, response["text"])

    # A proposal needs a UI to approve it. Run in the background so a voice
    # session is back to listening as soon as the reply has been spoken,
    # rather than waiting out another LLM call it has no part in.
    if should_propose and websocket is not None:
        user_text = payload.get("text", "")
        _in_background(propose_skill(websocket, response["trace"], past_trace, user_text, pending_skills))


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
    try:
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
    except ConnectionClosed:
        # The connection closed before this fire-and-forget follow-up could
        # be sent -- same "additive, never lets a background extra blow up
        # the request" principle as speak().
        return
    log_event(
        "skill_proposed",
        id=proposal_id,
        name=definition["name"],
        confident=past_trace is not None,
        uncertain_count=len(definition.get("uncertain_params", [])),
    )


# --- voice session hooks ----------------------------------------------------


async def run_voice_command(text: str) -> None:
    """A live-session utterance becomes a command on the primary connection,
    through exactly the path a push-to-talk transcript takes."""
    websocket, pending, pending_skills = _primary()
    log_event("message_in", payload={"type": "voice", "text": text})
    await process_command(websocket, {"text": text}, pending, pending_skills, origin="voice_session")


async def answer_confirmation_by_voice(text: str) -> bool:
    """While a confirmation is pending, the session only lets a clear yes/no
    through (as an ordinary voice command, which the shared path then turns
    into the answer). Anything else is shown but not run: starting a second
    command while the first is paused on a question would break "one command
    in flight", and a stray remark shouldn't be sent to the model as a
    request anyway. The confirmation stays open for another try or a click."""
    websocket, pending, pending_skills = _primary()
    if not pending or interpret_yes_no(text) is None:
        await _send(websocket, {"type": "transcript", "text": text, "origin": "voice_session"})
        log_event("voice_confirmation_unclear", text=text)
        return False
    _in_background(
        process_command(websocket, {"text": text}, pending, pending_skills, origin="voice_session")
    )
    return True


def confirmation_pending() -> bool:
    _, pending, _ = _primary()
    return any(not future.done() for future in pending.values())


def _voice_source_factory():
    """LEUTHERIA_VOICE_SOURCE=<wav> replaces the mic with a WAV file (played
    at real speed, then silence forever) so the full server can be exercised
    end to end without touching the microphone."""
    wav = os.environ.get("LEUTHERIA_VOICE_SOURCE")
    if not wav:
        return None
    return lambda settings: audio_io.wav_frames(wav, realtime=1.0, pad_silence=True)


def start_voice_session() -> "voice_session.VoiceSession":
    session = voice_session.VoiceSession(
        voice_settings.load(),
        emit=broadcast,
        on_command=run_voice_command,
        on_confirmation_answer=answer_confirmation_by_voice,
        confirmation_pending=confirmation_pending,
        source_factory=_voice_source_factory(),
    )
    session.start()
    return session


# --- connection handling ----------------------------------------------------


async def handler(websocket):
    # Per-connection state. `pending` maps a confirmation id to the Future its
    # resolution unblocks; `pending_skills` maps a proposal id to the skill
    # definition awaiting an approve/decline. Commands are dispatched as
    # background tasks (not awaited inline) specifically so this loop stays
    # free to read an incoming "confirm"/"skill_response" while a command is
    # paused mid-flight waiting on one.
    pending: dict = {}
    pending_skills: dict = {}
    entry = (websocket, pending, pending_skills)
    _connections.append(entry)

    # A (re)connecting UI learns the current voice state straight away rather
    # than showing a stale indicator until the next transition.
    if voice_session.current() is not None:
        await _send(websocket, voice_session.current().snapshot())

    try:
        await _read_loop(websocket, pending, pending_skills)
    finally:
        _connections.remove(entry)


async def _read_loop(websocket, pending: dict, pending_skills: dict):
    async for raw in websocket:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.send(json.dumps({"type": "error", "text": "invalid JSON"}))
            continue

        if payload.get("type") in control.CONTROL_TYPES:
            # Management traffic from the UI (read the catalog, toggle a
            # capability, delete a skill, drive the voice session). Answered
            # synchronously and tagged with the caller's request_id so the
            # Electron side can resolve the right promise -- these interleave
            # freely with a command that's still mid-flight.
            result = control.handle(payload)
            await websocket.send(
                json.dumps({"type": "control_result", "request_id": payload.get("request_id"), **result})
            )
            continue

        if payload.get("type") == "confirm":
            future = pending.get(payload.get("id"))
            if future and not future.done():
                future.set_result(
                    {"approved": bool(payload.get("approved")), "remember": payload.get("remember")}
                )
            continue

        if payload.get("type") == "skill_response":
            definition = pending_skills.pop(payload.get("id"), None)
            if definition and payload.get("approved"):
                finalized = skill_learning.finalize_definition(definition, payload.get("resolutions", {}))
                register_skill(finalized)
                log_event("skill_registered", name=finalized["name"])
                await websocket.send(json.dumps({"type": "skill_saved", "name": finalized["name"]}))
            elif definition:
                log_event(
                    "skill_declined",
                    name=definition["name"],
                    source_signature=definition.get("source_signature"),
                )
            continue

        log_event("message_in", payload=payload)
        _in_background(process_command(websocket, payload, pending, pending_skills))


async def main():
    async with websockets.serve(handler, HOST, PORT, max_size=MAX_MESSAGE_SIZE):
        # The voice session starts whether or not it's enabled -- disabled it
        # just sits in "off" until set_voice_settings or a manual start. It
        # owns its own failures (no mic, model download failed): they're
        # logged as voice_error and leave it "off", never take the server down.
        try:
            start_voice_session()
        except Exception as e:
            log_event("voice_error", stage="startup", error=str(e))
        print(f"AGENT_READY ws://{HOST}:{PORT}", flush=True)
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
