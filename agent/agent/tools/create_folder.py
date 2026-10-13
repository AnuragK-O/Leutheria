from pathlib import Path


def create_folder(path: str) -> dict:
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "path must be a non-empty string"}
    target = Path(path).expanduser()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        # exist_ok only covers an existing *folder*. A file of that name (or
        # a file where a parent folder should be) lands here.
        return {"ok": False, "error": f"{target} already exists and is a file, not a folder"}
    except NotADirectoryError:
        return {"ok": False, "error": f"cannot create {target}: part of that path is a file, not a folder"}
    except OSError as e:
        return {"ok": False, "error": f"cannot create {target}: {e.strerror or e}"}
    return {"ok": True, "message": f"created {target}"}
