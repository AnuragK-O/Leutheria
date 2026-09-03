from pathlib import Path


def append_text(path: str, text: str) -> dict:
    """Append-only by design -- can never truncate or overwrite existing
    content, so this stays a "safe" tool unlike a general-purpose file write."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as f:
        f.write(text + "\n")
    return {"ok": True, "message": f"appended to {target}"}
