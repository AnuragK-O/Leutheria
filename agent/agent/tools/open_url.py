from agent.core import bounded

TIMEOUT = 15.0


def open_url(url: str) -> dict:
    code, _, stderr = bounded.run(["open", url], timeout=TIMEOUT)
    if code != 0:
        return {"ok": False, "error": stderr or f"could not open {url}"}
    return {"ok": True, "message": f"opened {url}"}
