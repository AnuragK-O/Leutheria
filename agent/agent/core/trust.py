import json
import os
import time
from pathlib import Path

# Persisted trust survives agent restarts; session trust doesn't. Both are
# keyed on the exact (tool, args) pair -- not just the tool name -- so
# trusting one specific command (e.g. a fixed `git init` in a fixed
# directory) can never blanket-trust a *different* invocation of the same
# tool with different, unreviewed args (e.g. a different `rm` target).
TRUST_FILE = Path(__file__).resolve().parent.parent.parent / "trusted_commands.json"

_session_trust: set = set()
_permanent_trust: set = set()


def _key(tool_name: str, args: dict) -> str:
    return json.dumps({"tool": tool_name, "args": args}, sort_keys=True)


# Why the trust file was not used, or None. A file that doesn't parse, isn't
# a list, or holds a malformed entry means *nothing* from it is trusted --
# never "whatever parsed". A malformed entry used to raise at import and
# stop the agent starting. The broken file is kept aside, not overwritten,
# the next time an approval is saved. Nothing in the agent can write this
# file except trust_always() below: core/path_policy.py refuses every tool
# write to it.
load_error = None


def _read_entries() -> list:
    """The file's entries, all valid, or raises ValueError."""
    entries = json.loads(TRUST_FILE.read_text())
    if not isinstance(entries, list):
        raise ValueError("not a list")
    for entry in entries:
        if not (isinstance(entry, dict) and isinstance(entry.get("tool"), str) and isinstance(entry.get("args"), dict)):
            raise ValueError("malformed entry")
    return entries


def _load_permanent_trust() -> None:
    global load_error
    _permanent_trust.clear()
    load_error = None
    if not TRUST_FILE.exists() and not TRUST_FILE.is_symlink():
        return
    try:
        entries = _read_entries()
    except (OSError, UnicodeDecodeError, ValueError) as e:  # JSONDecodeError is a ValueError
        load_error = f"trusted_commands.json could not be used ({type(e).__name__}: {e}); nothing is trusted"
        from agent.core.logging_util import log_event

        log_event("trust_load_failed", error=load_error)
        return
    for entry in entries:
        _permanent_trust.add(_key(entry["tool"], entry["args"]))


def _stored_entries() -> list:
    """What to rewrite the file from. If the file on disk is broken, it is
    moved aside first and only the approvals held in memory are written."""
    if not TRUST_FILE.exists():
        return []
    try:
        return _read_entries()
    except (OSError, UnicodeDecodeError, ValueError):
        TRUST_FILE.rename(TRUST_FILE.with_name(f"{TRUST_FILE.name}.corrupt-{int(time.time())}"))
        return [json.loads(key) for key in sorted(_permanent_trust)]


def _write(entries: list) -> None:
    tmp = TRUST_FILE.with_name(TRUST_FILE.name + ".tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    os.replace(tmp, TRUST_FILE)


_load_permanent_trust()


def is_trusted(tool_name: str, args: dict) -> bool:
    key = _key(tool_name, args)
    return key in _session_trust or key in _permanent_trust


def trust_for_session(tool_name: str, args: dict) -> None:
    _session_trust.add(_key(tool_name, args))


def trust_always(tool_name: str, args: dict) -> None:
    """Persist to disk immediately, and also cover this session right away."""
    key = _key(tool_name, args)
    _permanent_trust.add(key)
    _session_trust.add(key)

    entries = _stored_entries()
    if not any(_key(e["tool"], e["args"]) == key for e in entries):
        entries.append({"tool": tool_name, "args": args})
    _write(entries)


def list_trusted() -> list:
    """Every standing approval, for display in the Library's Permissions view.
    `scope` distinguishes the two: "always" survives a restart and is what the
    revoke button acts on; "session" evaporates on its own."""
    entries = []
    for key in sorted(_permanent_trust):
        entry = json.loads(key)
        entries.append({"tool": entry["tool"], "args": entry["args"], "scope": "always", "key": key})
    for key in sorted(_session_trust - _permanent_trust):
        entry = json.loads(key)
        entries.append({"tool": entry["tool"], "args": entry["args"], "scope": "session", "key": key})
    return entries


def revoke(key: str) -> dict:
    """Withdraw a standing approval by its exact key, so that command has to be
    confirmed again next time. Clears it from both tiers -- revoking a
    persisted approval that's also live this session should take effect now,
    not after a restart."""
    found = key in _permanent_trust or key in _session_trust
    _permanent_trust.discard(key)
    _session_trust.discard(key)

    if TRUST_FILE.exists():
        _write([e for e in _stored_entries() if _key(e["tool"], e["args"]) != key])

    return {"ok": found, "error": None if found else "no such trusted command"}
