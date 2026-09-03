import shutil
from pathlib import Path


def move_file(source: str, destination: str) -> dict:
    src = Path(source).expanduser()
    dst = Path(destination).expanduser()

    if not src.exists():
        return {"ok": False, "error": f"{src} does not exist"}
    if dst.exists():
        # Refuse rather than silently overwrite -- this is what keeps
        # move_file "safe" instead of needing a confirmation prompt.
        return {"ok": False, "error": f"{dst} already exists -- refusing to overwrite"}

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"ok": True, "message": f"moved {src} to {dst}"}
