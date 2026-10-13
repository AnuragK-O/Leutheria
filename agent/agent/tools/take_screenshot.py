"""Save a screenshot of the whole screen as a PNG -- never over an existing file.

"Safe" has to mean it cannot destroy anything, and `screencapture <path>`
replaces whatever is at <path>. So the name is claimed first, with
O_CREAT | O_EXCL: the existence check and the creation are one step in the
kernel, the same way write_new_file does it. screencapture then writes into
the empty file this call just made, and nothing else.

  save_path given   an existing file (or anything else there) is refused,
                    like move_file and copy_file -- the caller chose the name,
                    so the caller picks another.
  no save_path      the timestamped Desktop name is this tool's own choice,
                    so a second shot in the same second gets " (2)", " (3)"...
"""

import os
import subprocess
from datetime import datetime
from pathlib import Path

TIMEOUT = 20.0  # seconds; screencapture is normally well under one
MAX_DEFAULT_TRIES = 50


def _claim(path: Path) -> None:
    """Create `path` as a new empty file, or raise FileExistsError."""
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))


def _capture(path: Path) -> subprocess.CompletedProcess:
    """The one place the screen is read -- the check scripts replace it."""
    return subprocess.run(["screencapture", "-x", str(path)], capture_output=True, text=True, timeout=TIMEOUT)


def take_screenshot(save_path: str = None) -> dict:
    if save_path is not None and (not isinstance(save_path, str) or not save_path.strip()):
        return {"ok": False, "error": "save_path must be a file path"}

    try:
        if save_path:
            path = Path(save_path.strip()).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                _claim(path)
            except FileExistsError:
                return {"ok": False, "error": f"{path} already exists -- refusing to overwrite"}
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            folder = Path("~/Desktop").expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            for attempt in range(1, MAX_DEFAULT_TRIES + 1):
                path = folder / (f"screenshot-{stamp}.png" if attempt == 1 else f"screenshot-{stamp} ({attempt}).png")
                try:
                    _claim(path)
                    break
                except FileExistsError:
                    continue
            else:
                return {"ok": False, "error": "could not find a free name for the screenshot on the Desktop"}
    except OSError as e:
        return {"ok": False, "error": f"cannot save a screenshot there: {e}"}

    # From here the file at `path` is ours and empty; on any failure it is
    # removed again, so a refused capture never leaves a zero-byte PNG behind.
    try:
        result = _capture(path)
        error = None if result.returncode == 0 else (result.stderr.strip() or "the screenshot could not be taken")
    except subprocess.TimeoutExpired:
        error = f"the screenshot did not finish within {TIMEOUT:g} seconds"
    except OSError as e:
        error = str(e)
    if error is None and (not path.is_file() or path.stat().st_size == 0):
        error = "the screenshot came out empty; check Screen Recording permission in System Settings"
    if error is not None:
        try:
            if path.is_file() and path.stat().st_size == 0:
                path.unlink()
        except OSError:
            pass
        return {"ok": False, "error": error}
    return {"ok": True, "path": str(path)}
