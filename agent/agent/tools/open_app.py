from agent.core import bounded

TIMEOUT = 15.0  # `open` returns once the app has been asked to launch


def open_app(name: str) -> dict:
    code, _, stderr = bounded.run(["open", "-a", name], timeout=TIMEOUT)
    if code != 0:
        return {"ok": False, "error": stderr or f"could not open {name}"}
    return {"ok": True, "message": f"opened {name}"}
