from pathlib import Path

MAX_CHARS = 1_000_000  # a text file written by the assistant, not a data dump


def write_new_file(path: str, content: str) -> dict:
    """Create a text file with the given content -- only if nothing is at
    that path yet.

    This is what makes a file *write* safe without a confirmation: the file
    is opened with mode "x" (O_CREAT | O_EXCL), so the existence check and
    the creation are one atomic step in the kernel. There is no window in
    which an existing file -- or a dangling symlink pointing somewhere else
    -- can be truncated or replaced. Changing an existing file is
    append_text's job (add only) or the user's.

    Contract other skills build on: returns {"ok": True, "path": <absolute
    path>} on success, {"ok": False, "error": ...} if the path exists.
    """
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "path must be a non-empty string"}
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be text"}
    if len(content) > MAX_CHARS:
        return {"ok": False, "error": f"content is longer than {MAX_CHARS} characters"}

    target = Path(path).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Kept apart from the open below: mkdir raises FileExistsError when
        # a *parent* is a file, which is not "the file already exists".
        return {"ok": False, "error": f"cannot create the folder {target.parent}: {e}"}
    try:
        with target.open("x", encoding="utf-8", newline="") as f:
            f.write(content)
    except FileExistsError:
        return {"ok": False, "error": f"{target} already exists -- refusing to overwrite"}
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(target), "message": f"created {target}"}
