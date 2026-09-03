import subprocess


def get_clipboard() -> dict:
    result = subprocess.run(["pbpaste"], capture_output=True, text=True)
    return {"ok": True, "text": result.stdout}
