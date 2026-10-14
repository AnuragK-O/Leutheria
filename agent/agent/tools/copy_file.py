import os
import shutil
import stat
import time
from pathlib import Path

# A copy is a bounded job (review M3): without limits, copying /dev/zero or
# a folder with a symlink loop ran until the disk filled.
MAX_BYTES = 2_000_000_000
MAX_FILES = 20_000
MAX_SECONDS = 120.0


def _measure(src: Path) -> tuple:
    """(bytes, files, error) for a folder, without following symlinks.
    Anything that isn't a plain file, folder or link refuses the copy."""
    total, count = 0, 0
    deadline = time.monotonic() + MAX_SECONDS / 4
    stack = [str(src)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError as e:
            return 0, 0, f"cannot read {current}: {e.strerror or e}"
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                stack.append(entry.path)
            elif stat.S_ISREG(st.st_mode):
                total += st.st_size
                count += 1
            elif not stat.S_ISLNK(st.st_mode):
                return 0, 0, f"{entry.path} is a device, socket or pipe, which can't be copied"
            if count > MAX_FILES or total > MAX_BYTES or time.monotonic() > deadline:
                return 0, 0, "that is too large to copy in one go"
    return total, count, None


def copy_file(source: str, destination: str) -> dict:
    src = Path(source).expanduser()
    dst = Path(destination).expanduser()

    try:
        st = src.stat()
    except OSError:
        return {"ok": False, "error": f"{src} does not exist"}
    if os.path.lexists(dst):
        # Refuse rather than silently overwrite -- this is what keeps
        # copy_file "safe" instead of needing a confirmation prompt.
        # lexists, not exists: a dangling symlink is still "something there",
        # and copying onto it would write wherever it points.
        return {"ok": False, "error": f"{dst} already exists -- refusing to overwrite"}

    if stat.S_ISDIR(st.st_mode):
        _, _, error = _measure(src)
        if error:
            return {"ok": False, "error": error}
    elif not stat.S_ISREG(st.st_mode):
        return {"ok": False, "error": f"{src} is not a regular file or folder"}
    elif st.st_size > MAX_BYTES:
        return {"ok": False, "error": "that file is too large to copy in one go"}

    dst.parent.mkdir(parents=True, exist_ok=True)
    if stat.S_ISDIR(st.st_mode):
        # symlinks=True: a link is copied as a link, never followed out of
        # the folder being copied.
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=True)
    return {"ok": True, "message": f"copied {src} to {dst}"}
