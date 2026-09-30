import asyncio
import json
import uuid

from agent.core import preferences, session, trust, ui_access
from agent.core.logging_util import log_event
from agent.core.tts import speak
from agent.skills.registry import SKILLS
from agent.tools.registry import TOOLS

CONFIRMATION_TIMEOUT = 120  # seconds -- an unanswered prompt is treated as declined


async def dispatch(
    tool_name: str, args: dict, websocket=None, pending: dict = None, intro_text: str = ""
) -> dict:
    skill = SKILLS.get(tool_name)
    tool = TOOLS.get(tool_name)
    if skill is None and tool is None:
        return {"ok": False, "error": f"unknown tool: {tool_name}"}

    # A capability the user switched off in the Library isn't offered to the
    # model in the first place, so this is belt-and-braces: it also catches a
    # stale plan, a generated skill's hardcoded step, or a direct dispatch.
    if not preferences.is_enabled(tool_name):
        log_event("dispatch_blocked_disabled", tool=tool_name, args=args)
        return {"ok": False, "error": f"{tool_name} is currently disabled by the user"}

    # A "scoped" tool drives the GUI, so it's gated per target app for a
    # stretch of time rather than per call -- see _check_scope.
    if tool is not None and tool["safety"] == "scoped":
        allowed, error = await _check_scope(tool_name, tool, args, websocket, pending, intro_text)
        if not allowed:
            return {"ok": False, "error": error}
        return _run_tool(tool, args)

    # Built-in policy: destructive tools ask, everything else doesn't. Skills
    # default to not asking because each of their *steps* dispatches back
    # through here and confirms on its own. Either default can be overridden
    # per capability from the Library ("always ask before using this one").
    default_confirm = tool is not None and tool["safety"] == "destructive"
    if preferences.requires_confirmation(tool_name, default_confirm) and not trust.is_trusted(
        tool_name, args
    ):
        approved, remember = await _confirm(tool_name, args, websocket, pending, intro_text)

        if remember == "session":
            trust.trust_for_session(tool_name, args)
        elif remember == "always":
            trust.trust_always(tool_name, args)

        if not approved:
            log_event("tool_declined", tool=tool_name, args=args)
            return {
                "ok": False,
                "error": f"{tool_name} was declined by the user (or the confirmation prompt timed out)",
            }

    if skill is not None:
        return await skill["fn"](args, websocket, pending)

    return _run_tool(tool, args)


def _run_tool(tool: dict, args: dict) -> dict:
    try:
        return tool["fn"](**args)
    except TypeError as e:
        return {"ok": False, "error": str(e)}


def _scope_target(tool: dict, args: dict) -> str:
    """Which app a scoped call would actually act on.

    Declared per tool in the registry: "frontmost" means the call lands on
    whatever is in front at this instant (type_text), otherwise it names the
    argument holding the app name (focus_app's "name"). Resolving it here
    rather than inside the tool is what stops a tool from being able to act
    on an app other than the one it was checked against.
    """
    if tool.get("target") == "frontmost":
        return ui_access.frontmost_app()
    return (args.get(tool.get("target", ""), "") or "").strip()


async def _check_scope(
    tool_name: str, tool: dict, args: dict, websocket, pending: dict, intro_text: str
) -> tuple:
    """Gate a GUI tool on a live grant for its target app.

    Per-call confirmation is the wrong shape here -- nobody approves two
    hundred keystrokes -- so approval is per app and time-boxed: one prompt
    when the agent first reaches for an app, then it can drive that app
    freely until the grant lapses. The denylist is checked first and is not
    overridable by any grant or preference; everything else about a spoken
    "yes" here reuses the existing confirmation round-trip.
    """
    app = _scope_target(tool, args)
    if not app:
        return False, f"{tool_name} could not determine which app it would act on"

    if ui_access.is_denied(app):
        log_event("scope_denied", tool=tool_name, app=app)
        return False, f"{app} can never be controlled by Leutheria"

    if session.has_grant(app):
        return True, None

    # An explicit "don't ask about this one" from the Library turns the grant
    # requirement off, the same way it can for run_command. The denylist
    # above still stands.
    if not preferences.requires_confirmation(tool_name, True):
        session.grant(app)
        return True, None

    spoken = intro_text.strip() or f"Do you want to let me control {app}?"
    approved, remember = await _confirm(tool_name, {**args, "app": app}, websocket, pending, spoken)
    if not approved:
        log_event("scope_declined", tool=tool_name, app=app)
        return False, f"permission to control {app} was declined by the user"

    # The card offers "this session" / "always"; for an app grant both mean
    # "until the agent restarts", since no grant is ever written to disk.
    # A plain approval is the time-boxed default.
    expires = session.grant(app, duration=None if remember else session.GRANT_DURATION)
    log_event("scope_granted", tool=tool_name, app=app, expires=expires)
    return True, None


def _fallback_prompt(tool_name: str, args: dict) -> str:
    """Spoken when the model gave no explanation of its own. It has to say
    *what* will happen: the old "I want to run run_command" named the tool
    and left out the only part a listener can judge (BUGS.md #22)."""
    if tool_name == "run_command" and args.get("cmd"):
        return f"I want to run the command: {args['cmd'][:120]}. Should I go ahead?"
    return f"I want to use {tool_name.replace('_', ' ')}. Should I go ahead?"


async def _confirm(tool_name: str, args: dict, websocket, pending: dict, intro_text: str = "") -> tuple:
    if websocket is None or pending is None:
        # No interactive channel to confirm through (e.g. dispatch called
        # directly outside a live connection) -- fail closed, not open.
        return False, None

    confirmation_id = str(uuid.uuid4())
    future = asyncio.get_running_loop().create_future()
    pending[confirmation_id] = future

    await websocket.send(
        json.dumps(
            {
                "type": "confirmation_required",
                "id": confirmation_id,
                "tool": tool_name,
                "args": args,
            }
        )
    )
    log_event("confirmation_requested", id=confirmation_id, tool=tool_name, args=args)

    # Speak the prompt so this can be answered by voice alone. Prefer the
    # model's own accompanying explanation for this turn (SYSTEM_PROMPT.md
    # already asks it to briefly explain a destructive action before taking
    # it) over a generic fallback -- it's more natural and specific.
    spoken = intro_text.strip() or _fallback_prompt(tool_name, args)
    await speak(websocket, spoken)

    try:
        result = await asyncio.wait_for(future, timeout=CONFIRMATION_TIMEOUT)
        return bool(result.get("approved")), result.get("remember")
    except asyncio.TimeoutError:
        return False, None
    finally:
        pending.pop(confirmation_id, None)
