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

# Every open connection, oldest first, as (websocket, pending,
# pending_skills) -- for a UI connection `websocket` is its _MirroringSocket,
# for an overlay the raw socket. Confirmations and skill proposals are per-connection, but
# the voice session isn't tied to any connection -- so voice commands run on
# the most recently opened one (the "primary"). With nothing connected they
# run with websocket=None, which makes any destructive step fail closed in
# dispatcher._confirm(); that's the intended behaviour, not a gap.
_connections: list = []

# The subset of _connections whose client said {"type":"hello","role":"overlay"}
# (keyed by the entry's websocket object). An overlay is a second *view* of
# the voice session -- the native Qt activation overlay -- not a place a
# conversation lives: it never becomes the primary, so replies, confirmations
# and skill proposals keep going to the Electron window. It receives every
# broadcast() plus a mirror of what the primary is told (_mirror_to_overlays).
_overlays: set = set()

OVERLAY_ROLE = "overlay"

# What an overlay needs to draw, beyond the broadcasts: the confirmation card
# (required/resolved), and what was heard and answered in the voice session.
# "speaking" is included because the overlay shows the line being spoken,
# which for a confirmation prompt never arrives as a "response" at all.
_MIRRORED_TYPES = {"confirmation_required", "confirmation_resolved", "transcript", "response", "speaking"}
# Of those, conversation proper is only mirrored for the live session -- a
# typed command's reply belongs to the chat window alone.
_SESSION_ONLY_TYPES = {"transcript", "response"}

# Fire-and-forget tasks are kept referenced until done -- asyncio only holds
# weak references, so an unreferenced task can be collected mid-flight.
_background: set = set()


def get_backend():
    global _backend
    if _backend is None:
        _backend = AnthropicBackend()
    return _backend


def _primary() -> tuple:
    # Newest connection that isn't an overlay. Note an overlay counts as an
    # ordinary connection for the one round trip between its handshake and
    # its hello; the hello is the first frame it sends, so in practice that
    # window is well under a millisecond on loopback.
    for entry in reversed(_connections):
        if entry[0] not in _overlays:
            return entry
    return None, {}, {}


class _MirroringSocket:
    """Stands in for a UI connection's websocket everywhere a command runs.

    Why a wrapper rather than a server-level "send to primary and overlays"
    helper: the sends an overlay needs are spread across modules that only
    ever see the `websocket` they were handed -- dispatcher._confirm() sends
    confirmation_required itself, tts.speak() sends "speaking", skills pass
    the websocket straight through to dispatch(). Threading an overlay list
    through all of that would change every skill's run(args, websocket,
    pending) signature (CLAUDE.md). Wrapping the one object they already
    share mirrors every send at the source, including sends added later.

    Only mirrors while this connection is the primary -- an overlay can only
    answer the primary's confirmations, so showing another window's card
    would offer buttons that can't work. Everything but send() is delegated.
    Reading (`async for`) goes through the raw websocket, not this."""

    def __init__(self, websocket):
        self._websocket = websocket

    def __getattr__(self, name):
        return getattr(self._websocket, name)

    async def send(self, message):
        # Primary first: if the UI is gone this raises, and the overlay must
        # not show a card for a confirmation nobody can see the details of.
        await self._websocket.send(message)
        if _primary()[0] is self:
            await _mirror_to_overlays(message)


async def _mirror_to_overlays(message) -> None:
    if not _overlays:
        return
    if isinstance(message, (str, bytes)):
        try:
            message = json.loads(message)
        except (ValueError, TypeError):
            return
    if not isinstance(message, dict):
        return
    kind = message.get("type")
    if kind not in _MIRRORED_TYPES:
        return
    if kind in _SESSION_ONLY_TYPES and message.get("origin") != "voice_session":
        return
    for websocket, _, _ in list(_connections):
        if websocket in _overlays:
            await _send(websocket, message)


def _in_background(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _send(websocket, message: dict) -> None:
    """Send, tolerating a missing or already-closed connection -- a voice
    command can outlive the window it was reported to, and nothing about the
    command itself depends on the UI hearing about it."""
    if websocket is None:
        # No UI connected: a voice command still runs, and an overlay can
        # still show what was heard and answered.
        await _mirror_to_overlays(message)
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
        await _send(_raw(websocket), message)


def _raw(websocket):
    """The real socket behind a connection entry -- for a send that already
    reaches every client, so the primary's mirror mustn't repeat it."""
    return websocket._websocket if isinstance(websocket, _MirroringSocket) else websocket


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


async def decline_pending_by_dismissal() -> None:
    """The session was dismissed while a confirmation was open: decline it now
    rather than leave it waiting, unseen, until it times out (BUGS.md #22).
    Sent as by:"voice" so both UI cards settle the way a spoken "no" does --
    but without a transcript, since the user never said "no"."""
    websocket, pending, _ = _primary()
    for confirmation_id, future in list(pending.items()):
        if future.done():
            continue
        future.set_result({"approved": False, "remember": None})
        log_event("confirmation_declined_by_dismissal", id=confirmation_id)
        await _send(
            websocket,
            {"type": "confirmation_resolved", "id": confirmation_id, "approved": False, "by": "voice"},
        )


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
        on_dismiss_pending=decline_pending_by_dismissal,
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
    # Every connection starts as a UI connection (no hello = exactly the old
    # behaviour); a hello with role "overlay" swaps the entry's socket for
    # the raw one and marks it (_set_role).
    entry = (_MirroringSocket(websocket), pending, pending_skills)
    _connections.append(entry)

    # A (re)connecting UI learns the current voice state straight away rather
    # than showing a stale indicator until the next transition.
    if voice_session.current() is not None:
        await _send(websocket, voice_session.current().snapshot())

    connection = {"entry": entry}
    try:
        await _read_loop(websocket, connection)
    finally:
        _connections.remove(connection["entry"])
        _overlays.discard(connection["entry"][0])


def _set_role(connection: dict, role) -> None:
    """Handle {"type":"hello","role":...}. Only "overlay" changes anything;
    any other role (or none) is an ordinary UI connection, as before."""
    websocket, pending, pending_skills = connection["entry"]
    is_overlay = role == OVERLAY_ROLE
    if is_overlay == (websocket in _overlays):
        return
    raw = _raw(websocket)
    # An overlay gets its raw socket: nothing it is sent is re-mirrored.
    new_entry = (raw if is_overlay else _MirroringSocket(raw), pending, pending_skills)
    index = _connections.index(connection["entry"])
    _connections[index] = new_entry
    _overlays.discard(websocket)
    if is_overlay:
        _overlays.add(raw)
    connection["entry"] = new_entry
    log_event("client_hello", role=role or "ui")


def _find_pending_confirmation(confirmation_id):
    """An overlay has no confirmations of its own: its answer is for one the
    primary is waiting on. Looks through every UI connection (primary first)
    rather than the primary alone, so a card shown just before another window
    connected -- and took over as primary -- can still be answered."""
    ui_entries = [e for e in _connections if e[0] not in _overlays]
    for _, pending, _ in reversed(ui_entries):
        future = pending.get(confirmation_id)
        if future is not None:
            return future
    return None


async def _handle_confirm(connection: dict, payload: dict) -> None:
    websocket, pending, _ = connection["entry"]
    confirmation_id = payload.get("id")
    answer = {"approved": bool(payload.get("approved")), "remember": payload.get("remember")}

    if websocket in _overlays:
        future = _find_pending_confirmation(confirmation_id)
        if future is None or future.done():
            return
        future.set_result(answer)
        log_event("confirmation_answered_by_overlay", id=confirmation_id, approved=answer["approved"])
        # Every client, not just the primary: the Electron window and any
        # other overlay all have a card for this id to settle. Raw sends, so
        # the primary's mirror doesn't deliver it to overlays a second time.
        resolved = {"type": "confirmation_resolved", "id": confirmation_id, "approved": answer["approved"],
                    "by": "overlay"}
        for other, _, _ in list(_connections):
            await _send(_raw(other), resolved)
        return

    future = pending.get(confirmation_id)
    if future and not future.done():
        future.set_result(answer)
        # The UI settles its own cards on a click (main.js), and has never
        # been sent a resolution for one -- so only overlays are told.
        for other, _, _ in list(_connections):
            if other in _overlays:
                await _send(
                    other,
                    {"type": "confirmation_resolved", "id": confirmation_id, "approved": answer["approved"],
                     "by": "ui"},
                )


async def _refresh_input_devices(websocket, request_id) -> None:
    """Re-read the system's device list (NOTES #6), then answer like a plain
    list_input_devices. With a live voice session the session does it, since
    only it can close the mic stream first; otherwise PortAudio is idle."""
    session = voice_session.current()
    try:
        if session is not None:
            await session.refresh_devices()
        else:
            await asyncio.get_running_loop().run_in_executor(None, audio_io.reinitialize_devices)
        result = control.handle({"type": "list_input_devices"})
    except Exception as e:
        result = {"ok": False, "error": f"couldn't refresh input devices: {e}"}
    await _send(websocket, {"type": "control_result", "request_id": request_id, **result})


# What an overlay may send besides hello and control messages. It's a view,
# not a conversation: a command from it would run on a connection with no
# chat to show the reply in and no card of its own to confirm through.
_OVERLAY_REJECTED = "overlay connections can only send hello, confirm and control messages"


async def _read_loop(websocket, connection: dict):
    async for raw in websocket:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.send(json.dumps({"type": "error", "text": "invalid JSON"}))
            continue
        if not isinstance(payload, dict):
            await websocket.send(json.dumps({"type": "error", "text": "expected a JSON object"}))
            continue

        if payload.get("type") == "hello":
            _set_role(connection, payload.get("role"))
            continue

        # Re-read every message: a hello swaps the entry.
        entry_socket, pending, pending_skills = connection["entry"]
        is_overlay = entry_socket in _overlays

        if payload.get("type") == "list_input_devices" and payload.get("refresh"):
            # A refresh briefly closes and reopens the mic, so unlike the
            # other control messages it can't be answered inline; it replies
            # (same request_id) when done, without holding up this loop.
            _in_background(_refresh_input_devices(websocket, payload.get("request_id")))
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
            await _handle_confirm(connection, payload)
            continue

        if is_overlay:
            await _send(websocket, {"type": "error", "text": _OVERLAY_REJECTED})
            continue

        if payload.get("type") == "skill_response":
            definition = pending_skills.pop(payload.get("id"), None)
            if definition and payload.get("approved"):
                finalized = skill_learning.finalize_definition(definition, payload.get("resolutions", {}))
                saved = register_skill(finalized)
                if saved.get("ok"):
                    log_event("skill_registered", name=finalized["name"], id=saved.get("id"))
                    await websocket.send(json.dumps({"type": "skill_saved", "name": finalized["name"]}))
                else:
                    # Validated at proposal time, so this means the user's
                    # answers or a same-named skill saved since broke it.
                    log_event("skill_register_rejected", name=finalized.get("name"), reason=saved.get("error"))
                    # Tell the user too: they pressed Approve, and silence
                    # would read as success (BUGS.md #26 follow-up).
                    await websocket.send(
                        json.dumps(
                            {"type": "skill_save_failed", "name": finalized.get("name"), "error": saved.get("error")}
                        )
                    )
            elif definition:
                log_event(
                    "skill_declined",
                    name=definition["name"],
                    source_signature=definition.get("source_signature"),
                )
            continue

        log_event("message_in", payload=payload)
        # entry_socket (the mirroring wrapper), not the raw one: this is the
        # object dispatch(), skills and speak() will send through.
        _in_background(process_command(entry_socket, payload, pending, pending_skills))


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
