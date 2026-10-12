import os
import shutil
import stat
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from agent.tools import fs_common

MAX_MEMBERS = 20_000
MAX_BYTES = 2_000_000_000  # uncompressed -- the zip-bomb ceiling
MAX_SECONDS = 120.0
CHUNK = 1024 * 1024

ARCHIVE_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".tar", ".zip")
# Finder's resource-fork sidecars: noise, never what the user wanted out.
IGNORED_TOP = "__MACOSX"
IGNORED_NAMES = frozenset({".DS_Store"})


class _Unsafe(Exception):
    pass


def _safe_parts(name: str) -> tuple:
    """Validate one member name and return its path components.

    This is the zip-slip guard. A member is refused -- and with it the whole
    archive -- if its name is absolute, has a drive letter, contains a NUL,
    or has any ".." component. It is deliberately a refusal, not a
    sanitiser: an archive carrying "../../.zshrc" is hostile or broken, and
    quietly writing that entry somewhere "safe" would report success for
    something nobody asked for.
    """
    if "\x00" in name:
        raise _Unsafe("an entry name contains a NUL byte")
    normalised = name.replace("\\", "/")
    if normalised.startswith("/") or (len(normalised) > 1 and normalised[1] == ":"):
        raise _Unsafe(f"entry {name!r} has an absolute path")
    parts = tuple(p for p in PurePosixPath(normalised).parts if p not in ("", "."))
    if any(p == ".." for p in parts):
        raise _Unsafe(f"entry {name!r} would be written outside the destination folder")
    return parts


def _ignored(parts: tuple) -> bool:
    return not parts or parts[0] == IGNORED_TOP or parts[-1] in IGNORED_NAMES


def _default_destination(archive: Path) -> Path:
    lowered = archive.name.lower()
    for suffix in ARCHIVE_SUFFIXES:
        if lowered.endswith(suffix) and len(archive.name) > len(suffix):
            return archive.parent / archive.name[: -len(suffix)]
    return archive.parent / f"{archive.name} extracted"


def _plan_zip(archive: zipfile.ZipFile):
    """[(parts, is_dir, declared_size, executable, info)], skipped."""
    plan, skipped = [], 0
    for info in archive.infolist():
        parts = _safe_parts(info.filename)
        if _ignored(parts):
            continue
        mode = info.external_attr >> 16
        if stat.S_IFMT(mode) == stat.S_IFLNK:
            skipped += 1
            continue
        plan.append((parts, info.is_dir(), info.file_size, bool(mode & 0o100), info))
    return plan, skipped


def _plan_tar(archive: tarfile.TarFile, deadline: float):
    plan, skipped, seen = [], 0, 0
    for member in archive:
        seen += 1
        if seen > MAX_MEMBERS * 2 or time.monotonic() > deadline:
            raise _Unsafe("the archive has too many entries")
        parts = _safe_parts(member.name)
        if _ignored(parts):
            continue
        if member.isdir():
            plan.append((parts, True, 0, False, member))
        elif member.isreg():
            plan.append((parts, False, member.size, bool(member.mode & 0o100), member))
        else:
            # Symlinks, hard links, devices, FIFOs: never created.
            skipped += 1
    return plan, skipped


def _write_members(plan, open_member, dest: Path, deadline: float) -> tuple:
    written = 0
    files = 0
    root = os.path.realpath(dest)
    for parts, is_dir, _, executable, handle in plan:
        target = dest.joinpath(*parts)
        # Belt and braces behind _safe_parts: nothing this call creates is a
        # symlink, so a resolved path can only leave dest if that reasoning
        # is wrong somewhere -- in which case stop rather than write.
        resolved = os.path.realpath(target)
        if resolved != root and not resolved.startswith(root + os.sep):
            raise _Unsafe(f"entry {'/'.join(parts)!r} would be written outside the destination folder")
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = open_member(handle)
        if source is None:
            continue
        # "xb": two entries with the same name must not overwrite each other.
        with source, open(target, "xb") as out:
            while True:
                if time.monotonic() > deadline:
                    raise _Unsafe(f"extracting took longer than {int(MAX_SECONDS)} seconds")
                chunk = source.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                # Declared sizes come from the archive and can lie; this is
                # the count of bytes actually produced.
                if written > MAX_BYTES:
                    raise _Unsafe(f"the archive expands to more than {fs_common.human_size(MAX_BYTES)}")
                out.write(chunk)
        if executable:
            os.chmod(target, 0o755)
        files += 1
    return files, written


def extract_archive(archive: str, destination: str = None) -> dict:
    """Extract a .zip or .tar(.gz/.bz2/.xz) archive into a NEW folder.

    Safe by construction:
    * the destination folder is created by this call with an exclusive
      mkdir, so nothing that already exists is ever written into, merged
      with or replaced;
    * every entry name is validated before anything is written (zip-slip),
      and one bad entry refuses the whole archive;
    * links and special files are never created; files are written with
      plain permissions (the executable bit is the only one carried over);
    * entry count, total size and time are capped (zip bombs).
    If extraction fails part-way, the folder this call created is removed --
    it only ever contains what this call wrote.
    """
    src = Path(archive).expanduser()
    if not src.is_file():
        return {"ok": False, "error": f"{src} is not a file"}

    dest = Path(os.path.abspath(Path(destination).expanduser())) if destination else _default_destination(src)
    if os.path.lexists(dest):
        return {"ok": False, "error": f"{dest} already exists -- refusing to extract into an existing folder"}

    deadline = time.monotonic() + MAX_SECONDS
    opened = None
    created = False
    try:
        if zipfile.is_zipfile(src):
            opened = zipfile.ZipFile(src)
            plan, skipped = _plan_zip(opened)
            open_member = opened.open
        elif tarfile.is_tarfile(src):
            opened = tarfile.open(src, "r:*")
            plan, skipped = _plan_tar(opened, deadline)
            open_member = opened.extractfile
        else:
            return {"ok": False, "error": f"{src.name} is not a zip or tar archive"}

        if len(plan) > MAX_MEMBERS:
            return {"ok": False, "error": f"the archive has more than {MAX_MEMBERS} entries"}
        declared = sum(size for _, is_dir, size, _, _ in plan if not is_dir)
        if declared > MAX_BYTES:
            return {"ok": False, "error": f"the archive expands to more than {fs_common.human_size(MAX_BYTES)}"}

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.mkdir()
        except FileExistsError:
            return {"ok": False, "error": f"{dest} already exists -- refusing to extract into an existing folder"}
        created = True
        files, written = _write_members(plan, open_member, dest, deadline)
    except _Unsafe as e:
        if created:
            shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "error": f"refused to extract {src.name}: {e}"}
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError, RuntimeError, NotImplementedError) as e:
        if created:
            shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "error": f"could not extract {src.name}: {e}"}
    finally:
        if opened is not None:
            opened.close()

    top_level = sorted(os.listdir(dest))
    message = f"extracted {files} file{'' if files == 1 else 's'} from {src.name} into {dest}"
    if skipped:
        message += f"; left out {skipped} link{'' if skipped == 1 else 's'} or special file{'' if skipped == 1 else 's'}"
    return {
        "ok": True,
        "path": str(dest),
        "file_count": files,
        "size_bytes": written,
        "size": fs_common.human_size(written),
        "skipped": skipped,
        "top_level": top_level[:20],
        "message": message,
    }
