from pathlib import Path


def list_files(path: str) -> dict:
    target = Path(path).expanduser()
    if not target.is_dir():
        return {"ok": False, "error": f"{target} is not a directory"}
    return {"ok": True, "files": sorted(p.name for p in target.iterdir())}
