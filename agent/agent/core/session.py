"""Live session state: what the agent is currently working in, what was just
said, and which apps it's allowed to drive.

Until now every request was independent -- agent_loop.run() built a fresh
`messages` list per message, so "add that too" had no antecedent and every
GUI action would have to re-name its target app. This holds the two pieces
of state that make a multi-minute working session coherent:

  focus    -- the app (and when) the agent last successfully focused, injected
              into the system prompt so a follow-up doesn't have to re-specify
              where it's going.
  history  -- a rolling window of plain user/assistant turns.
  grants   -- which apps may be driven by "scoped" tools, and until when.

Deliberately a module-level singleton rather than per-connection state. The
alternative is threading a session object through dispatch() and therefore
through every skill's run(args, websocket, pending) signature, which is a lot
of churn for a single-user local agent with one window.

Grants are in-memory only and are never persisted: restarting the agent
revokes everything. That's the safe direction to fail, and it's a meaningful
difference from trust.py's "always" scope, which does survive a restart --
trust there is keyed to an exact (tool, args) pair, whereas a grant here is
open-ended ("anything typed into Bear for the next 15 minutes") and shouldn't
outlive the session that asked for it.
"""

import time

GRANT_DURATION = 15 * 60  # seconds an app stays drivable after one approval
CONVERSATION_IDLE = 10 * 60  # seconds of silence that ends a conversation
MAX_HISTORY_TURNS = 12  # plain turns kept (user + assistant counted separately)

_focus: dict = {}
_grants: dict = {}  # canonical app name -> unix expiry
_history: list = []
_last_activity: float = 0.0


# --- app grants -------------------------------------------------------------


def grant(app_name: str, duration: float = GRANT_DURATION) -> float:
    """Allow the scoped tools to drive an app. `duration` of None means "until
    the agent restarts" -- the longest a grant can ever last, since none of
    them are written to disk. That's what the confirmation card's "always"
    maps to: it can't mean permanently, so it means for this session, and the
    Permissions view says so rather than implying more."""
    expires = None if duration is None else time.time() + duration
    _grants[app_name] = expires
    return expires


def has_grant(app_name: str) -> bool:
    if app_name not in _grants:
        return False
    expiry = _grants[app_name]
    if expiry is None:
        return True
    if expiry < time.time():
        del _grants[app_name]
        return False
    return True


def revoke(app_name: str) -> bool:
    return _grants.pop(app_name, None) is not None


def active_grants() -> list:
    now = time.time()
    for name in [n for n, expiry in _grants.items() if expiry is not None and expiry < now]:
        del _grants[name]
    return [
        {"app": name, "expires_in": None if expiry is None else int(expiry - now)}
        for name, expiry in sorted(_grants.items())
    ]


# --- focus ------------------------------------------------------------------


def set_focus(app_name: str) -> None:
    global _focus
    _focus = {"app": app_name, "since": time.time()}


def current_focus() -> dict:
    return dict(_focus)


def clear_focus() -> None:
    global _focus
    _focus = {}


# --- conversation history ---------------------------------------------------


def _expire_if_idle() -> None:
    """A conversation is over once it's been quiet long enough. Without this,
    a request hours later would silently inherit the last one's context and
    resolve "it"/"that" against something the user has long forgotten."""
    global _history
    if _last_activity and time.time() - _last_activity > CONVERSATION_IDLE:
        _history = []
        clear_focus()


def history() -> list:
    _expire_if_idle()
    return list(_history)


def remember(role: str, text: str) -> None:
    global _last_activity
    _expire_if_idle()
    if text:
        _history.append({"role": role, "content": text})
        del _history[:-MAX_HISTORY_TURNS]
    _last_activity = time.time()


def clear_history() -> None:
    global _history
    _history = []


def context_note() -> str:
    """The live-state paragraph appended to the system prompt. Returns "" when
    there's nothing to say, so a cold session's prompt is unchanged."""
    lines = []
    if _focus:
        minutes = int((time.time() - _focus["since"]) // 60)
        ago = "just now" if minutes < 1 else f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        lines.append(
            f"You are currently working in {_focus['app']} (focused {ago}). "
            "If the user refers to a document, note, or window without naming an app, "
            "they almost certainly mean this one."
        )
    grants = active_grants()
    if grants:
        allowed = ", ".join(g["app"] for g in grants)
        lines.append(f"You currently have permission to control: {allowed}.")
    return "\n".join(lines)
