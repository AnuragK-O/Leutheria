import shutil
from pathlib import Path


def copy_file(source: str, destination: str) -> dict:
    src = Path(source).expanduser()
    dst = Path(destination).expanduser()

    if not src.exists():
        return {"ok": False, "error": f"{src} does not exist"}
    if dst.exists():
        # Refuse rather than silently overwrite -- this is what keeps
        # copy_file "safe" instead of needing a confirmation prompt.
        return {"ok": False, "error": f"{dst} already exists -- refusing to overwrite"}

    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return {"ok": True, "message": f"copied {src} to {dst}"}
