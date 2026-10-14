"""In-process countdown timers (set_timer / list_timers / cancel_timer).

A timer is an asyncio task on the server's event loop, so it lives exactly as
long as the agent process: nothing is written to disk, and a restart forgets
every timer. That is said in the tool descriptions and in every result,
because "I set a timer" that silently never fires is the worst outcome here.

When one fires it does two things, both through the paths everything else
uses rather than around them:

  * a notification, via dispatch("show_notification") -- so a user who
    switched that tool off in the Library gets no banner from a timer either;
  * a short spoken line, via tts.speak() -- never any other playback. The
    voice session is half-duplex and only speak() tells it the speaker is
    live; a chime played any other way would be heard by the mic and acted on.

Neither needs a UI: speak(None, ...) still talks. When a UI *is* connected
the line goes out on the primary connection (server._primary()), so the
window and the overlay show `speaking` the way they do for any reply. That
lookup happens here, at fire time, precisely so nothing about timers has to
be threaded through dispatch() or a skill's run(args, websocket, pending).
"""

import asyncio
import sys
import time
from datetime import datetime

from agent.core.logging_util import log_event

MAX_TIMERS = 20  # running at once
MAX_SECONDS = 24 * 60 * 60
MAX_LABEL = 60
MAX_MESSAGE = 200

# The countdown is against the wall clock, re-checked at least this often.
# asyncio.sleep() counts monotonic time, which on macOS stops while the Mac
# is asleep: a single sleep(1800) set before a 20-minute nap would fire 20
# minutes late. Polling means it fires within POLL_S of waking instead.
POLL_S = 5.0

LIFETIME_NOTE = "Timers only last while Leutheria keeps running; quitting or restarting it clears them."

_timers: dict = {}  # id -> timer dict (including its task)
_firing: set = set()  # tasks past their countdown, kept referenced until done
_next_id = 0

# Swappable for tests, like tts._player.
_clock = time.time


class TimerError(Exception):
    pass


def describe_duration(seconds: float) -> str:
    """'10 minutes', '1 hour 30 minutes', '45 seconds' -- for speech, so no
    abbreviations and at most two units."""
    if seconds < 1:
        return "less than a second"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    for value, unit in ((hours, "hour"), (minutes, "minute"), (secs, "second")):
        if value:
            parts.append(f"{value} {unit}{'' if value == 1 else 's'}")
    return " ".join(parts[:2])


def _clock_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%I:%M %p").lstrip("0")


def _public(timer: dict) -> dict:
    remaining = max(0.0, timer["fires_at"] - _clock())
    return {
        "timer_id": timer["id"],
        "label": timer["label"],
        "duration": describe_duration(timer["seconds"]),
        "remaining_seconds": round(remaining, 1),
        "remaining": describe_duration(remaining),
        "ends_at": _clock_time(timer["fires_at"]),
    }


def create(seconds: float, label: str = "", message: str = "") -> dict:
    """Start a timer on the running event loop. Raises TimerError for anything
    it won't do; returns the public view of the new timer."""
    global _next_id
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds != seconds:
        raise TimerError("the duration must be a number")
    if seconds <= 0:
        raise TimerError("the duration must be more than zero")
    if seconds > MAX_SECONDS:
        raise TimerError("a timer can be at most 24 hours long")
    if len(_timers) >= MAX_TIMERS:
        raise TimerError(f"there are already {MAX_TIMERS} timers running; cancel one first")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        raise TimerError("timers can only be set while the agent is running") from None

    _next_id += 1
    timer = {
        "id": str(_next_id),
        "label": " ".join(str(label or "").split())[:MAX_LABEL],
        "message": " ".join(str(message or "").split())[:MAX_MESSAGE],
        "seconds": float(seconds),
        "fires_at": _clock() + float(seconds),
    }
    _timers[timer["id"]] = timer
    timer["task"] = loop.create_task(_countdown(timer))
    log_event("timer_set", id=timer["id"], seconds=timer["seconds"], label=timer["label"])
    return _public(timer)


def active() -> list:
    """Running timers, soonest first."""
    return [_public(t) for t in sorted(_timers.values(), key=lambda t: t["fires_at"])]


def cancel(timer_id: str = "", label: str = "", cancel_all: bool = False) -> list:
    """Cancel by id, by label, or all of them. With neither id nor label and
    exactly one timer running, that one is meant ("cancel the timer").
    Returns the public views of what was cancelled; raises TimerError when
    the request doesn't pick anything out."""
    if cancel_all:
        chosen = list(_timers.values())
    elif timer_id:
        timer = _timers.get(str(timer_id).strip())
        if timer is None:
            raise TimerError(f"there is no running timer with id {timer_id}")
        chosen = [timer]
    elif label:
        wanted = " ".join(str(label).split()).lower()
        chosen = [t for t in _timers.values() if t["label"].lower() == wanted]
        if not chosen:
            chosen = [t for t in _timers.values() if wanted in t["label"].lower()]
        if not chosen:
            raise TimerError(f"there is no running timer labelled {label}")
    elif len(_timers) == 1:
        chosen = list(_timers.values())
    elif not _timers:
        raise TimerError("there are no timers running")
    else:
        raise TimerError("more than one timer is running; say which one by its id or label, or cancel all")

    cancelled = []
    for timer in chosen:
        _timers.pop(timer["id"], None)
        cancelled.append(_public(timer))
        timer["task"].cancel()
        log_event("timer_cancelled", id=timer["id"], label=timer["label"])
    return cancelled


def reset() -> None:
    """Cancel everything without logging -- for tests."""
    for timer in list(_timers.values()):
        timer["task"].cancel()
    _timers.clear()


async def _countdown(timer: dict) -> None:
    while True:
        remaining = timer["fires_at"] - _clock()
        if remaining <= 0:
            break
        await asyncio.sleep(min(remaining, POLL_S))
    # Off the list before anything is announced, and announced from a task of
    # its own: cancel() can then never interrupt a timer halfway through
    # speaking, and list_timers never shows one that has already gone off.
    _timers.pop(timer["id"], None)
    task = asyncio.get_running_loop().create_task(_fire(timer))
    _firing.add(task)
    task.add_done_callback(_firing.discard)


# A reminder's message is chosen by the model when the timer is set and spoken
# much later with nobody watching the screen, so it must not be able to pass
# for something else -- above all a confirmation prompt ("Should I go ahead?")
# that a "yes" would answer (review L4). It is always introduced by this fixed
# lead-in, cut short, and stripped of question marks.
REMINDER_LEAD_IN = "Timer reminder:"
SPOKEN_MESSAGE_MAX = 120


def _clean_message(message: str) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in message)
    text = " ".join(text.replace("?", ".").split())
    if len(text) > SPOKEN_MESSAGE_MAX:
        text = text[:SPOKEN_MESSAGE_MAX].rsplit(" ", 1)[0] + "..."
    return text


def spoken_line(timer: dict) -> str:
    if timer["message"]:
        cleaned = _clean_message(timer["message"])
        if cleaned:
            return f"{REMINDER_LEAD_IN} {cleaned}"
    if timer["label"]:
        return f"Your {timer['label']} timer is done."
    # "Your 10 minute timer", not "your 10 minutes timer".
    length = " ".join(word[:-1] if word.endswith("s") and word != "less" else word
                      for word in describe_duration(timer["seconds"]).split())
    return f"Your {length} timer is done."


def _primary_connection() -> tuple:
    """(websocket, pending) of the UI the user is looking at, or (None, None).
    Read from the server only if the server is what's running -- importing it
    here would drag the whole voice stack into anything that sets a timer."""
    server = sys.modules.get("agent.core.server")
    if server is None:
        return None, None
    websocket, pending, _ = server._primary()
    return websocket, (pending if websocket is not None else None)


async def _fire(timer: dict) -> None:
    from agent.core import tts
    from agent.core.dispatcher import dispatch

    line = spoken_line(timer)
    websocket, pending = _primary_connection()
    try:
        title = f"Timer: {timer['label']}" if timer["label"] else "Timer done"
        shown = await dispatch("show_notification", {"title": title, "message": line}, websocket, pending)
    except Exception as e:  # a timer must still be heard if the banner blows up
        shown = {"ok": False, "error": str(e)}
    log_event(
        "timer_fired",
        id=timer["id"],
        label=timer["label"],
        seconds=timer["seconds"],
        late_by=round(_clock() - timer["fires_at"], 2),
        notified=bool(shown.get("ok")),
        notify_error=shown.get("error"),
    )
    # speak() swallows its own failures; it also queues behind a reply that
    # is being spoken right now rather than talking over it.
    await tts.speak(websocket, line)
