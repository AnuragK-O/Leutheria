import subprocess


def open_url(url: str) -> dict:
    result = subprocess.run(["open", url], capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()}
    return {"ok": True, "message": f"opened {url}"}
