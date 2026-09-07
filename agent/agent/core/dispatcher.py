import asyncio
import json
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from agent.core import preferences, trust
from agent.core.action import ExecutionMode, RiskLevel, StructuredAction, normalize_tool_action
from agent.core.logging_util import log_event
from agent.core.permissions import (
    PermissionScope,
    evaluate_action_authorization,
    grant_permission,
)
from agent.core.trace import default_trace_manager
from agent.core.tts import speak
from agent.skills.registry_v2 import SKILLS
from agent.tools.registry import TOOLS

CONFIRMATION_TIMEOUT = 120  # seconds


async def dispatch(
    tool_name: str,
    args: dict,
    websocket=None,
    pending: dict = None,
    intro_text: str = "",
    task_id: Optional[str] = None,
    execution_mode: str = "copilot",
    step_index: int = 0,
) -> dict:
    skill = SKILLS.get(tool_name)
    tool = TOOLS.get(tool_name)
    if skill is None and tool is None:
        return {"ok": False, "error": f"unknown tool: {tool_name}"}

    # Normalize into StructuredAction
    action: StructuredAction = normalize_tool_action(tool_name, args, reason=intro_text)

    # Record proposed step in persistent trace
    step_record = None
    if task_id:
        step_record = default_trace_manager.record_step_proposed(
            task_id=task_id,
            step_index=step_index,
            tool_name=tool_name,
            tool_args=args,
            action=action,
            reasoning=intro_text,
        )

    # Check if disabled
    if not preferences.is_enabled(tool_name):
        log_event("dispatch_blocked_disabled", tool=tool_name, args=args)
        err = f"{tool_name} is currently disabled by the user"
        if step_record:
            default_trace_manager.record_step_result(step_record.step_id, {"ok": False, "error": err}, success=False)
        return {"ok": False, "error": err}

    mode_enum = ExecutionMode(execution_mode.lower()) if execution_mode.lower() in [e.value for e in ExecutionMode] else ExecutionMode.COPILOT

    # Evaluate authorization
    authorized, reason = evaluate_action_authorization(
        tool_name=tool_name,
        action=action,
        mode=mode_enum,
        task_id=task_id,
    )

    if not authorized:
        approved, remember, correction = await _confirm(
            tool_name, args, action, mode_enum, websocket, pending, intro_text
        )

        # Handle human correction if provided
        if correction:
            log_event("human_correction_received", tool=tool_name, original_args=args, correction=correction)
            if task_id:
                default_trace_manager.record_correction(
                    task_id=task_id,
                    step_id=step_record.step_id if step_record else None,
                    proposed_action=action.to_dict(),
                    user_correction=correction,
                    correction_type="parameter_override" if isinstance(correction, dict) else "guidance",
                    context=f"Corrected during approval of {tool_name}",
                )
            if isinstance(correction, dict):
                args = {**args, **correction}
                action = normalize_tool_action(tool_name, args, reason=intro_text)
                approved = True

        if remember == "session":
            trust.trust_for_session(tool_name, args)
            for perm in action.required_permissions:
                grant_permission(perm, scope=PermissionScope.SESSION)
        elif remember == "always":
            trust.trust_always(tool_name, args)
            for perm in action.required_permissions:
                grant_permission(perm, scope=PermissionScope.PERMANENT)
        elif remember == "task" and task_id:
            for perm in action.required_permissions:
                grant_permission(perm, scope=PermissionScope.TASK, context_id=task_id)

        if not approved:
            log_event("tool_declined", tool=tool_name, args=args)
            err = f"{tool_name} was declined by the user ({reason})"
            if step_record:
                default_trace_manager.record_step_result(step_record.step_id, {"ok": False, "error": err}, success=False)
            return {"ok": False, "error": err}

    start_time = time.time()
    try:
        if skill is not None:
            # Skills execute their internal sub-steps
            result = await skill["fn"](args, websocket, pending)
        else:
            result = tool["fn"](**args)
    except TypeError as e:
        result = {"ok": False, "error": str(e)}
    except Exception as e:
        result = {"ok": False, "error": f"Execution error: {str(e)}"}

    duration_ms = (time.time() - start_time) * 1000.0
    success = isinstance(result, dict) and result.get("ok", True) is not False

    if step_record:
        default_trace_manager.record_step_result(step_record.step_id, result, success=success, duration_ms=duration_ms)

    return result


async def _confirm(
    tool_name: str,
    args: dict,
    action: StructuredAction,
    mode: ExecutionMode,
    websocket,
    pending: dict,
    intro_text: str = "",
) -> Tuple[bool, Optional[str], Optional[Any]]:
    if websocket is None or pending is None:
        return False, None, None

    confirmation_id = str(uuid.uuid4())
    future = asyncio.get_running_loop().create_future()
    pending[confirmation_id] = future

    payload = {
        "type": "confirmation_required",
        "id": confirmation_id,
        "tool": tool_name,
        "args": args,
        "action": action.to_dict(),
        "mode": mode.value,
        "risk": action.risk_level.value,
        "description": action.description,
        "required_permissions": action.required_permissions,
    }

    await websocket.send(json.dumps(payload))
    log_event("confirmation_requested", id=confirmation_id, tool=tool_name, args=args, risk=action.risk_level.value)

    spoken = intro_text.strip() or f"I want to run {tool_name}. Should I proceed?"
    await speak(websocket, spoken)

    try:
        result = await asyncio.wait_for(future, timeout=CONFIRMATION_TIMEOUT)
        approved = bool(result.get("approved"))
        remember = result.get("remember")
        correction = result.get("correction")
        return approved, remember, correction
    except asyncio.TimeoutError:
        return False, None, None
    finally:
        pending.pop(confirmation_id, None)
