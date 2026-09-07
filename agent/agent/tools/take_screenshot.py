import subprocess
from datetime import datetime
from pathlib import Path


def take_screenshot(save_path: str = None) -> dict:
    if save_path:
        path = Path(save_path).expanduser()
    else:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path(f"~/Desktop/screenshot-{timestamp}.png").expanduser()

    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["screencapture", "-x", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()}
    return {"ok": True, "path": str(path)}
