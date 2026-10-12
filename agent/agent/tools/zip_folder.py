import os
import stat
import time
import zipfile
from pathlib import Path

from agent.tools import fs_common

MAX_FILES = 20_000
MAX_BYTES = 1_000_000_000  # uncompressed; see the note on blocking in fs_common
MAX_SECONDS = 120.0
ALWAYS_SKIP = frozenset({".DS_Store"})


def _collect(source: Path, exclude: frozenset):
    """(files, folders, skipped_links, error). files are (abs_path, size).
    Symlinks are left out rather than followed or stored: following one can
    pull in a tree outside the folder being zipped, and a stored link is
    exactly what extract_archive refuses."""
    files, folders, skipped, total = [], [], 0, 0
    stack = [str(source)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            return None, None, 0, f"cannot read {current}: {e}"
        for entry in entries:
            if entry.name in exclude:
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                skipped += 1
            elif stat.S_ISDIR(st.st_mode):
                folders.append(entry.path)
                stack.append(entry.path)
            elif stat.S_ISREG(st.st_mode):
                files.append((entry.path, st.st_size))
                total += st.st_size
                if len(files) > MAX_FILES:
                    return None, None, 0, f"more than {MAX_FILES} files -- too large to zip in one go"
                if total > MAX_BYTES:
                    return None, None, 0, (
                        f"more than {fs_common.human_size(MAX_BYTES)} of files -- too large to zip in one go"
                    )
            else:
                skipped += 1
    return files, folders, skipped, None


def zip_folder(source: str, destination: str = None, exclude=None) -> dict:
    """Zip a folder (or a single file) into a new .zip archive.

    Safe by construction: the source is only read, and the archive is
    created with an exclusive open ("xb"), so an existing file at the
    destination can never be replaced -- the check and the create are one
    atomic step. If anything fails part-way, the only thing removed is the
    half-written archive this call itself just created.
    """
    src = Path(source).expanduser()
    if not src.exists() or src.is_symlink():
        return {"ok": False, "error": f"{src} does not exist"}
    src = Path(os.path.abspath(src))
    if not (src.is_dir() or src.is_file()):
        return {"ok": False, "error": f"{src} is not a file or folder"}

    if destination:
        dst = Path(os.path.abspath(Path(destination).expanduser()))
        if dst.is_dir():
            dst = dst / f"{src.name}.zip"
        elif dst.suffix.lower() != ".zip":
            dst = dst.with_name(dst.name + ".zip")
    else:
        dst = src.parent / f"{src.name}.zip"

    if os.path.lexists(dst):
        return {"ok": False, "error": f"{dst} already exists -- refusing to overwrite"}
    if src.is_dir() and (dst == src or src in dst.parents):
        return {"ok": False, "error": "the zip file cannot be created inside the folder it is zipping"}

    if isinstance(exclude, str):
        exclude = exclude.replace(",", " ").split()
    skip = ALWAYS_SKIP | frozenset(str(item) for item in (exclude or []))

    if src.is_dir():
        files, folders, skipped, error = _collect(src, skip)
        if error:
            return {"ok": False, "error": error}
        base = src.parent
    else:
        size = src.stat().st_size
        if size > MAX_BYTES:
            return {"ok": False, "error": f"larger than {fs_common.human_size(MAX_BYTES)} -- too large to zip"}
        files, folders, skipped, base = [(str(src), size)], [], 0, src.parent

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        handle = open(dst, "xb")
    except FileExistsError:
        return {"ok": False, "error": f"{dst} already exists -- refusing to overwrite"}
    except OSError as e:
        return {"ok": False, "error": str(e)}

    deadline = time.monotonic() + MAX_SECONDS
    try:
        with handle, zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            if src.is_dir():
                # Explicit folder entries, so empty folders (and an empty
                # source) survive the round trip.
                archive.writestr(f"{src.name}/", "")
                for folder in folders:
                    archive.writestr(os.path.relpath(folder, base).replace(os.sep, "/") + "/", "")
            for file_path, _ in files:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"zipping took longer than {int(MAX_SECONDS)} seconds")
                archive.write(file_path, os.path.relpath(file_path, base).replace(os.sep, "/"))
    except (OSError, TimeoutError, zipfile.BadZipFile, ValueError) as e:
        # Only ever the file this call created a few lines above.
        try:
            os.unlink(dst)
        except OSError:
            pass
        return {"ok": False, "error": f"could not finish the zip: {e}"}

    source_bytes = sum(size for _, size in files)
    zip_bytes = dst.stat().st_size
    message = (
        f"zipped {len(files)} file{'' if len(files) == 1 else 's'} from {src.name} into {dst.name}, "
        f"{fs_common.human_size(zip_bytes)}"
    )
    if skipped:
        message += f"; left out {skipped} link{'' if skipped == 1 else 's'}"
    return {
        "ok": True,
        "path": str(dst),
        "file_count": len(files),
        "source_bytes": source_bytes,
        "size_bytes": zip_bytes,
        "size": fs_common.human_size(zip_bytes),
        "skipped_links": skipped,
        "message": message,
    }
