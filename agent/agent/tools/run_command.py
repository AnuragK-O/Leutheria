import subprocess


def run_command(cmd: str) -> dict:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
