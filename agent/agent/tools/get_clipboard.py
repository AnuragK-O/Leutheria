from agent.core import bounded


def get_clipboard() -> dict:
    code, stdout, stderr = bounded.run(["pbpaste"], timeout=5.0)
    if code != 0:
        return {"ok": False, "error": stderr or "could not read the clipboard"}
    return {"ok": True, "text": stdout}
