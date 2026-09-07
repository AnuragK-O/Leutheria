from pathlib import Path

from send2trash import send2trash


def trash_file(path: str) -> dict:
    """Moves to the system Trash rather than deleting outright -- recoverable,
    which is why this is "safe" and doesn't need the confirmation flow that
    run_command's irreversible rm would."""
    target = Path(path).expanduser()
    if not target.exists():
        return {"ok": False, "error": f"{target} does not exist"}

    try:
        send2trash(str(target))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "message": f"moved {target} to Trash"}
