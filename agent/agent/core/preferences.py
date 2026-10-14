import json
import os
import time
from pathlib import Path

# User-set overrides on individual capabilities (tools and skills alike),
# edited from the desktop UI's Library view rather than from code.
#
# Two independent knobs per capability:
#   enabled                -- if False, it isn't offered to the model at all,
#                             and dispatch() refuses it even if something else
#                             somehow asks for it (defense in depth).
#   requires_confirmation  -- if set, overrides the built-in default of
#                             "destructive tools ask, everything else doesn't".
#                             Setting it True on a safe tool is how a user says
#                             "always check with me before using this one".
#
# Tools and skills share one namespace here because they already share one at
# the model boundary -- both are presented to Claude as plain tool schemas, so
# a name collision between the two would be a bug regardless.
PREFS_FILE = Path(__file__).resolve().parent.parent.parent / "preferences.json"

_overrides: dict = {}

# Why the file could not be used, or None. Set when preferences.json exists
# but isn't a valid settings file. It used to be ignored, which dropped
# every "disabled" and "always ask" at once -- one appended line of junk
# turned the user's restrictions off (review C2). Now, while it is set:
#
#   * every capability asks before it runs (requires_confirmation is True
#     for all of them), so nothing the user switched off or asked to be
#     asked about can run without a yes -- we can't know which those were,
#     so all of them are treated that way;
#   * the broken file is not overwritten by the next change made in the
#     Library: it is kept beside it as preferences.json.corrupt-<time> first;
#   * the inventory carries the reason, for the Library to show.
#
# A single malformed entry poisons only its own capability (ask-first),
# not the whole file.
load_error = None
_bad_entries: set = set()
_kept_broken = False


def _valid_entry(entry) -> bool:
    return isinstance(entry, dict) and all(
        key in ("enabled", "requires_confirmation") and (value is None or isinstance(value, bool))
        for key, value in entry.items()
    )


def _load() -> None:
    global load_error
    _overrides.clear()
    _bad_entries.clear()
    load_error = None
    if not PREFS_FILE.exists() and not PREFS_FILE.is_symlink():
        return
    try:
        loaded = json.loads(PREFS_FILE.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        load_error = f"preferences.json could not be read ({type(e).__name__}); every capability asks first until it is fixed and Leutheria restarts"
    else:
        if not isinstance(loaded, dict):
            load_error = "preferences.json is not a settings object; every capability asks first until it is fixed and Leutheria restarts"
        else:
            for name, entry in loaded.items():
                if _valid_entry(entry):
                    _overrides[name] = dict(entry)
                else:
                    _bad_entries.add(name)
    if load_error or _bad_entries:
        from agent.core.logging_util import log_event

        log_event("preferences_load_failed", error=load_error, bad_entries=sorted(_bad_entries))


def _save() -> None:
    global _kept_broken
    if (load_error or _bad_entries) and not _kept_broken and PREFS_FILE.exists():
        # Keep what the user had; a fix by hand may still recover it.
        PREFS_FILE.rename(PREFS_FILE.with_name(f"{PREFS_FILE.name}.corrupt-{int(time.time())}"))
        _kept_broken = True
        # load_error stays set: what the broken file said is still unknown,
        # so everything keeps asking until the agent restarts on the new file.
    tmp = PREFS_FILE.with_name(PREFS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(_overrides, indent=2, sort_keys=True))
    os.replace(tmp, PREFS_FILE)  # never a half-written file that would then load as corrupt


_load()


def overrides_for(name: str) -> dict:
    return _overrides.get(name, {})


def all_overrides() -> dict:
    return json.loads(json.dumps(_overrides))  # deep copy -- callers can't mutate our state


def is_enabled(name: str) -> bool:
    return bool(_overrides.get(name, {}).get("enabled", True))


def requires_confirmation(name: str, default: bool) -> bool:
    """Whether this capability should pause for user approval. `default` is the
    built-in policy (destructive tools ask); an override wins over it."""
    if load_error or name in _bad_entries:
        return True  # fail closed: see load_error
    value = _overrides.get(name, {}).get("requires_confirmation")
    return default if value is None else bool(value)


def set_preference(name: str, enabled=None, requires_confirmation=None) -> dict:
    _bad_entries.discard(name)  # the user just said what they want for this one
    entry = _overrides.setdefault(name, {})
    if enabled is not None:
        entry["enabled"] = bool(enabled)
    if requires_confirmation is not None:
        entry["requires_confirmation"] = bool(requires_confirmation)
    _save()
    return dict(entry)


def clear(name: str) -> None:
    """Drop every override for a capability, returning it to built-in defaults.
    Also used when a generated skill is deleted, so a later skill that happens
    to reuse the name doesn't silently inherit the dead one's settings."""
    if _overrides.pop(name, None) is not None:
        _save()
