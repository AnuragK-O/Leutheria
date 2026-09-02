from pathlib import Path


def create_folder(path: str) -> dict:
    target = Path(path).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "message": f"created {target}"}
