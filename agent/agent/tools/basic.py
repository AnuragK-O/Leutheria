import subprocess
from pathlib import Path


def open_app(name: str) -> dict:
    result = subprocess.run(["open", "-a", name], capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()}
    return {"ok": True, "message": f"opened {name}"}


def create_folder(path: str) -> dict:
    target = Path(path).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "message": f"created {target}"}


def list_files(path: str) -> dict:
    target = Path(path).expanduser()
    if not target.is_dir():
        return {"ok": False, "error": f"{target} is not a directory"}
    return {"ok": True, "files": sorted(p.name for p in target.iterdir())}


def run_command(cmd: str) -> dict:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
