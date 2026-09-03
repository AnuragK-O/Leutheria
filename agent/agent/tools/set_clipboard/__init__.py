import subprocess


def set_clipboard(text: str) -> dict:
    subprocess.run(["pbcopy"], input=text, text=True)
    return {"ok": True, "message": "copied to clipboard"}
