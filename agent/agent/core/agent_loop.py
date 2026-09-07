import asyncio
import json
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
import uuid

from agent.core.dispatcher import dispatch
from agent.core.logging_util import log_event
from agent.core.trace import default_trace_manager
from agent.skills.registry_v2 import anthropic_skill_schemas, get_all_manifests, record_skill_execution
from agent.skills.retrieval import find_matching_skill
from agent.tools.registry import anthropic_tool_schemas

MAX_TURNS = 6

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "SYSTEM_PROMPT.md"
SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text() if SYSTEM_PROMPT_PATH.exists() else None


async def run(
    backend,
    user_text: str,
    websocket=None,
    pending: dict = None,
    execution_mode: str = "copilot",
    task_id: Optional[str] = None,
) -> dict:
    """Drive the LLM <-> tool-dispatch loop for one user request with persistence,
    retrieval, and supervision modes.
    """
    tid = task_id or str(uuid.uuid4())
    start_time = time.time()

    provider_name = getattr(backend, "provider_name", "anthropic")
    model_name = getattr(backend, "model_name", "claude-opus-5")

    task = default_trace_manager.start_task(
        user_request=user_text,
        provider=provider_name,
        model=model_name,
        execution_mode=execution_mode,
        task_id=tid,
    )

    # 1. Semantic Skill Retrieval: Check if an existing reliable skill can fulfill this
    all_skills = get_all_manifests()
    match = find_matching_skill(user_text, all_skills, threshold=0.45)

    if match is not None:
        skill_name = match.skill.name
        log_event("skill_retrieved", skill=skill_name, confidence=match.confidence)
        
        # Send timeline notification to user
        if websocket:
            try:
                await websocket.send(json.dumps({
                    "type": "timeline_event",
                    "task_id": tid,
                    "event": "skill_matched",
                    "text": f"Found existing skill '{skill_name}' ({match.confidence * 100:.0f}% match · Mode: {match.skill.autonomy_tier})",
                }))
            except Exception:
                pass

        # Execute retrieved skill workflow
        skill_start = time.time()
        result = await dispatch(
            tool_name=skill_name,
            args=match.extracted_params,
            websocket=websocket,
            pending=pending,
            intro_text=f"Executing matched skill '{skill_name}'",
            task_id=tid,
            execution_mode=execution_mode,
            step_index=0,
        )

        skill_duration = (time.time() - skill_start) * 1000.0
        success = isinstance(result, dict) and result.get("ok", True) is not False
        task_record = default_trace_manager.get_task(tid)
        interventions = task_record.intervention_count if task_record else 0

        # Record run in skill registry statistics
        record_skill_execution(skill_name, tid, success=success, interventions=interventions, duration_ms=skill_duration)

        total_duration = (time.time() - start_time) * 1000.0
        default_trace_manager.complete_task(
            tid,
            status="completed" if success else "failed",
            outcome_summary=f"Executed skill {skill_name}",
            duration_ms=total_duration,
        )

        reply_text = (
            f"Completed workflow '{skill_name}' successfully."
            if success
            else f"Workflow '{skill_name}' failed: {result.get('error', 'unknown error')}"
        )
        return {
            "type": "response",
            "text": reply_text,
            "trace": [{"tool": skill_name, "args": match.extracted_params, "result": result}],
            "task_id": tid,
            "skill_used": skill_name,
            "mode": execution_mode,
        }

    # 2. General Agentic Planning Loop
    messages = [{"role": "user", "content": user_text}]
    tools = anthropic_tool_schemas() + anthropic_skill_schemas()
    loop = asyncio.get_running_loop()
    trace = []
    total_tokens = 0
    step_index = 0

    for _ in range(MAX_TURNS):
        turn = await loop.run_in_executor(None, backend.generate, messages, tools, SYSTEM_PROMPT)
        if turn.usage:
            total_tokens += turn.usage.get("input_tokens", 0) + turn.usage.get("output_tokens", 0)

        log_event(
            "llm_turn",
            text=turn.text,
            tool_calls=[{"name": c.name, "args": c.args} for c in turn.tool_calls],
        )

        if not turn.tool_calls:
            total_duration = (time.time() - start_time) * 1000.0
            default_trace_manager.complete_task(
                tid,
                status="completed",
                outcome_summary=turn.text[:200] if turn.text else "Finished without tool calls",
                duration_ms=total_duration,
                tokens_used=total_tokens,
            )
            return {
                "type": "response",
                "text": turn.text,
                "trace": trace,
                "task_id": tid,
                "mode": execution_mode,
            }

        messages.append({"role": "assistant", "content": turn.content})

        tool_results = []
        for call in turn.tool_calls:
            # Emit live timeline event
            if websocket:
                try:
                    await websocket.send(json.dumps({
                        "type": "timeline_event",
                        "task_id": tid,
                        "event": "step_start",
                        "tool": call.name,
                        "args": call.args,
                    }))
                except Exception:
                    pass

            result = await dispatch(
                tool_name=call.name,
                args=call.args,
                websocket=websocket,
                pending=pending,
                intro_text=turn.text,
                task_id=tid,
                execution_mode=execution_mode,
                step_index=step_index,
            )
            step_index += 1

            log_event("tool_call", tool=call.name, args=call.args, result=result)
            trace.append({"tool": call.name, "args": call.args, "result": result})

            # Emit step result timeline event
            if websocket:
                try:
                    await websocket.send(json.dumps({
                        "type": "timeline_event",
                        "task_id": tid,
                        "event": "step_complete",
                        "tool": call.name,
                        "ok": isinstance(result, dict) and result.get("ok", True) is not False,
                    }))
                except Exception:
                    pass

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": json.dumps(result),
                    "is_error": isinstance(result, dict) and result.get("ok") is False,
                }
            )
        messages.append({"role": "user", "content": tool_results})

    total_duration = (time.time() - start_time) * 1000.0
    default_trace_manager.complete_task(
        tid,
        status="failed",
        outcome_summary="Max turns reached without final answer",
        duration_ms=total_duration,
        tokens_used=total_tokens,
    )
    return {
        "type": "response",
        "text": "(gave up after too many tool calls without a final answer)",
        "trace": trace,
        "task_id": tid,
        "mode": execution_mode,
    }
