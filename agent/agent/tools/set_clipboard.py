import subprocess

TIMEOUT = 5.0


def set_clipboard(text: str) -> dict:
    try:
        result = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"the clipboard did not respond within {TIMEOUT:g} seconds"}
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() or "could not copy to the clipboard"}
    return {"ok": True, "message": "copied to clipboard"}
