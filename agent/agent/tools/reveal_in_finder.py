from agent.core import bounded
from agent.tools.open_file import resolve_existing

# Injection point for the check script, as in open_file.
_launch = bounded.run


def reveal_in_finder(path: str) -> dict:
    """Show a file or folder selected in a Finder window. `open -R` only ever
    selects: it never opens, launches or runs the thing, whatever it is, so
    unlike open_file this needs no rules about file types."""
    resolved, error = resolve_existing(path)
    if error:
        return {"ok": False, "error": error}
    # Resolved, so absolute: it can never be read as a flag.
    code, _, err = _launch(["open", "-R", str(resolved)], 10.0)
    if code != 0:
        return {"ok": False, "error": err or f"could not show {resolved.name} in Finder"}
    return {"ok": True, "path": str(resolved), "message": f"Showing {resolved.name} in Finder."}
