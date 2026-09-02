import subprocess


def open_app(name: str) -> dict:
    result = subprocess.run(["open", "-a", name], capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()}
    return {"ok": True, "message": f"opened {name}"}
