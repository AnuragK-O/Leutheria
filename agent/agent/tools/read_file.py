from pathlib import Path

MAX_CHARS = 20000  # cap so a huge file can't blow up the LLM context


def read_file(path: str) -> dict:
    target = Path(path).expanduser()
    if not target.is_file():
        return {"ok": False, "error": f"{target} is not a file"}

    try:
        text = target.read_text(errors="replace")
    except Exception as e:
        return {"ok": False, "error": str(e)}

    truncated = len(text) > MAX_CHARS
    return {"ok": True, "content": text[:MAX_CHARS], "truncated": truncated}
