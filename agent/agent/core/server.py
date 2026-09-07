import asyncio
import json
import uuid

import websockets
from websockets.exceptions import ConnectionClosed

from agent.core import agent_loop, control, skill_learning_v2
from agent.core.audio_input import transcribe_payload
from agent.core.dispatcher import dispatch
from agent.core.logging_util import log_event
from agent.core.tts import speak
from agent.core.voice_confirm import interpret_yes_no
from agent.providers import get_provider
from agent.skills.registry_v2 import register_skill

HOST = "127.0.0.1"
PORT = 8765
MAX_MESSAGE_SIZE = 20 * 1024 * 1024  # 20 MiB


def get_backend():
    return get_provider()


async def handle_message(payload: dict, websocket, pending: dict) -> dict:
    mode = payload.get("mode", "copilot")
    task_id = payload.get("task_id")

    if "tool" in payload:
        result = await dispatch(
            payload["tool"],
            payload.get("args", {}),
            websocket=websocket,
            pending=pending,
            task_id=task_id,
            execution_mode=mode,
        )
        log_event("tool_call", tool=payload["tool"], args=payload.get("args", {}), result=result)
        return {"type": "tool_result", "tool": payload["tool"], "result": result, "mode": mode}

    if payload.get("type") == "audio":
        text = await transcribe_payload(payload)
        log_event("stt_transcript", text=text)
        await websocket.send(json.dumps({"type": "transcript", "text": text}))

        if pending:
            resolution = interpret_yes_no(text)
            if resolution is not None:
                confirmation_id, future = next(reversed(pending.items()))
                if not future.done():
                    future.set_result({"approved": resolution, "remember": None, "correction": None})
                    log_event("confirmation_answered_by_voice", id=confirmation_id, approved=resolution)
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "confirmation_resolved",
                                "id": confirmation_id,
                                "approved": resolution,
                                "by": "voice",
                            }
                        )
                    )
                return {"type": "response", "text": "", "mode": mode}

        if not text:
            return {"type": "response", "text": "(didn't catch that -- heard nothing)", "mode": mode}
        return await agent_loop.run(
            get_backend(),
            text,
            websocket=websocket,
            pending=pending,
            execution_mode=mode,
            task_id=task_id,
        )

    text = payload.get("text", "")
    if not text:
        return {"type": "response", "text": "", "mode": mode}

    return await agent_loop.run(
        get_backend(),
        text,
        websocket=websocket,
        pending=pending,
        execution_mode=mode,
        task_id=task_id,
    )


async def process_command(websocket, payload: dict, pending: dict, pending_skills: dict) -> None:
    response = await handle_message(payload, websocket, pending)

    trace = response.get("trace") or []
    user_text = payload.get("text", "")
    task_id = response.get("task_id")

    log_event("message_out", response=response)
    await websocket.send(json.dumps(response))

    if response.get("type") == "response" and response.get("text"):
        await speak(websocket, response["text"])

    # If the completed task was a multi-step workflow without using an existing skill,
    # propose turning it into a declarative reusable skill
    if len(trace) >= 2 and not response.get("skill_used"):
        await propose_skill(websocket, trace, user_text, task_id, pending_skills)


async def propose_skill(websocket, trace: list, user_text: str, task_id: str, pending_skills: dict) -> None:
    loop = asyncio.get_running_loop()
    try:
        proposal = await loop.run_in_executor(
            None,
            skill_learning_v2.propose_skill_from_trace,
            get_backend(),
            trace,
            user_text,
            None,
            task_id,
        )
    except Exception as e:
        log_event("skill_proposal_error", error=str(e))
        return

    if proposal is None:
        return

    proposal_id = str(uuid.uuid4())
    pending_skills[proposal_id] = proposal
    try:
        await websocket.send(
            json.dumps(
                {
                    "type": "skill_proposed",
                    "id": proposal_id,
                    "name": proposal["name"],
                    "description": proposal["description"],
                    "parameters": proposal.get("parameters", {}),
                    "permissions": proposal.get("permissions", []),
                    "risk_level": proposal.get("risk_level", "medium"),
                    "steps": proposal.get("steps", []),
                    "uncertain_params": proposal.get("uncertain_params", []),
                }
            )
        )
    except ConnectionClosed:
        return

    log_event(
        "skill_proposed",
        id=proposal_id,
        name=proposal["name"],
        uncertain_count=len(proposal.get("uncertain_params", [])),
    )


async def handler(websocket):
    pending: dict = {}
    pending_skills: dict = {}

    async for raw in websocket:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.send(json.dumps({"type": "error", "text": "invalid JSON"}))
            continue

        if payload.get("type") in control.CONTROL_TYPES:
            result = control.handle(payload)
            await websocket.send(
                json.dumps({"type": "control_result", "request_id": payload.get("request_id"), **result})
            )
            continue

        if payload.get("type") == "confirm":
            future = pending.get(payload.get("id"))
            if future and not future.done():
                future.set_result(
                    {
                        "approved": bool(payload.get("approved")),
                        "remember": payload.get("remember"),
                        "correction": payload.get("correction"),
                    }
                )
            continue

        if payload.get("type") == "skill_response":
            definition = pending_skills.pop(payload.get("id"), None)
            if definition and payload.get("approved"):
                finalized = skill_learning_v2.finalize_learned_skill(definition, payload.get("resolutions", {}))
                register_skill(finalized)
                log_event("skill_registered", name=finalized.name)
                await websocket.send(json.dumps({"type": "skill_saved", "name": finalized.name, "id": finalized.id}))
            elif definition:
                log_event("skill_declined", name=definition.get("name"))
            continue

        log_event("message_in", payload=payload)
        asyncio.create_task(process_command(websocket, payload, pending, pending_skills))


async def main():
    async with websockets.serve(handler, HOST, PORT, max_size=MAX_MESSAGE_SIZE):
        print(f"AGENT_READY ws://{HOST}:{PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
