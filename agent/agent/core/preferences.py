import json
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


def _load() -> None:
    if not PREFS_FILE.exists():
        return
    try:
        loaded = json.loads(PREFS_FILE.read_text())
    except json.JSONDecodeError:
        return  # a corrupt prefs file falls back to defaults, never breaks startup
    if isinstance(loaded, dict):
        _overrides.update(loaded)


def _save() -> None:
    PREFS_FILE.write_text(json.dumps(_overrides, indent=2, sort_keys=True))


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
    value = _overrides.get(name, {}).get("requires_confirmation")
    return default if value is None else bool(value)


def set_preference(name: str, enabled=None, requires_confirmation=None) -> dict:
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
