"""Play/pause, next and previous, sent as the keyboard's own media keys.

Why media keys rather than AppleScripting Music or Spotify: `tell application
"Music"` triggers a per-app Automation permission prompt the first time, for
each player, at a moment nobody is looking at the screen -- and it only works
for the players someone thought to script. A media key goes to whatever
macOS considers the current player (Music, Spotify, a browser tab, a podcast
app) and needs only the Accessibility permission the GUI tools already use.

The cost is that it is exactly as blind as the physical key: there is no way
to ask "is anything playing" without Automation or a private framework, so
play and pause are one toggle, and the result says a key was sent, not that
music stopped (BUGS.md #13: "the call succeeded" is not "it did what was
meant").
"""

# NX_KEYTYPE_* from IOKit's ev_keymap.h. A fixed table: the only thing a
# caller chooses is which of these three keys, never a key code.
MEDIA_KEYS = {"play_pause": 16, "next": 17, "previous": 18}

SENT = {
    "play_pause": "Sent play/pause.",
    "next": "Skipped to the next track.",
    "previous": "Went back a track.",
}

NSEVENT_SYSTEM_DEFINED = 14
MEDIA_KEY_SUBTYPE = 8


def _can_post() -> bool:
    """Whether macOS will deliver events this process posts. Without the
    permission CGEventPost drops them silently, so ask first."""
    import Quartz

    return bool(Quartz.CGPreflightPostEventAccess())


def _media_key_events(key_code: int) -> list:
    """The key-down and key-up events for one press, built but not sent."""
    from AppKit import NSEvent

    events = []
    for state, flags in ((0xA, 0xA00), (0xB, 0xB00)):  # down, up
        events.append(
            NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                NSEVENT_SYSTEM_DEFINED, (0, 0), flags, 0, 0, None, MEDIA_KEY_SUBTYPE, (key_code << 16) | (state << 8), -1
            )
        )
    return events


def _post_media_key(key_code: int) -> None:
    """Send one press. Swapped for a recorder in the check script -- nothing
    there presses a real key."""
    import Quartz

    for event in _media_key_events(key_code):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event.CGEvent())


def media_control(action: str) -> dict:
    key = MEDIA_KEYS.get(str(action).strip().lower()) if isinstance(action, str) else None
    if key is None:
        return {"ok": False, "error": "action must be one of: play_pause, next, previous"}
    if not _can_post():
        return {
            "ok": False,
            "error": (
                "Accessibility permission is not granted, so media keys would be ignored. Enable it "
                "for the app that launched Leutheria in System Settings, Privacy and Security, Accessibility."
            ),
        }
    try:
        _post_media_key(key)
    except Exception as e:
        return {"ok": False, "error": f"could not send the media key: {e}"}
    return {
        "ok": True,
        "action": action.strip().lower(),
        "message": SENT[action.strip().lower()],
        "note": "This presses the media key; it cannot tell whether anything was playing.",
    }
