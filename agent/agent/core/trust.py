import json
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


def _load_permanent_trust() -> None:
    if not TRUST_FILE.exists():
        return
    try:
        entries = json.loads(TRUST_FILE.read_text())
    except json.JSONDecodeError:
        return
    for entry in entries:
        _permanent_trust.add(_key(entry["tool"], entry["args"]))


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

    entries = []
    if TRUST_FILE.exists():
        try:
            entries = json.loads(TRUST_FILE.read_text())
        except json.JSONDecodeError:
            entries = []
    entries.append({"tool": tool_name, "args": args})
    TRUST_FILE.write_text(json.dumps(entries, indent=2))


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
        try:
            entries = json.loads(TRUST_FILE.read_text())
        except json.JSONDecodeError:
            entries = []
        remaining = [e for e in entries if _key(e["tool"], e["args"]) != key]
        TRUST_FILE.write_text(json.dumps(remaining, indent=2))

    return {"ok": found, "error": None if found else "no such trusted command"}
