"""Unpack a .zip or .tar archive into a new folder -- as data, never as programs.

An archive is somebody else's files. Two things macOS uses to decide whether
a file may run are therefore not the archive's to choose (review C3: a zip
carrying run.command and Evil.app came out ready to launch, with a 0755 mode
and no quarantine):

  execute bit   never set. Every file is written with ordinary read/write
                permissions whatever mode the archive recorded, so a script
                or program that was unpacked cannot be run by opening it.
                Folders keep search permission (they are created by mkdir).
                The price is that a project's own scripts (./configure,
                gradlew) need a chmod +x before they run; the result says
                how many files that applies to.
  quarantine    carried over. If the archive has com.apple.quarantine (the
                mark browsers and mail put on downloads), every file and
                folder extracted gets the same value, as Archive Utility
                does, so Gatekeeper still treats the contents as downloaded.
                If the mark cannot be applied the extraction fails and is
                removed, rather than leaving unmarked files behind.

The quarantine attribute is read and written with getxattr/setxattr from
libc through ctypes: Python's os.getxattr/os.setxattr do not exist on macOS,
and the alternative, /usr/bin/xattr, would be one subprocess per extracted
file (up to MAX_MEMBERS of them) inside a 120 second budget.
"""

import ctypes
import ctypes.util
import io
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


QUARANTINE = b"com.apple.quarantine"
_XATTR_NOFOLLOW = 0x0001
_libc = None


class _Unsafe(Exception):
    pass


def _xattr_lib():
    global _libc
    if _libc is None:
        lib = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
        # ssize_t getxattr(path, name, value, size, u_int32_t position, int options)
        lib.getxattr.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
        ]
        lib.getxattr.restype = ctypes.c_ssize_t
        # int setxattr(path, name, value, size, u_int32_t position, int options)
        lib.setxattr.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
        ]
        lib.setxattr.restype = ctypes.c_int
        _libc = lib
    return _libc


def read_quarantine(path) -> bytes:
    """The file's com.apple.quarantine value, or None if it has none. The
    path is not followed if it is a symlink."""
    lib = _xattr_lib()
    raw = os.fsencode(path)
    size = lib.getxattr(raw, QUARANTINE, None, 0, 0, _XATTR_NOFOLLOW)
    if size < 0:
        return None  # ENOATTR, or a filesystem with no extended attributes
    buffer = ctypes.create_string_buffer(max(size, 1))
    size = lib.getxattr(raw, QUARANTINE, buffer, size, 0, _XATTR_NOFOLLOW)
    return buffer.raw[:size] if size >= 0 else None


def write_quarantine(path, value: bytes) -> None:
    """Set com.apple.quarantine on one path, or raise OSError."""
    lib = _xattr_lib()
    if lib.setxattr(os.fsencode(path), QUARANTINE, value, len(value), 0, _XATTR_NOFOLLOW) != 0:
        code = ctypes.get_errno()
        raise OSError(code, f"could not mark {path} as downloaded: {os.strerror(code)}")


class _DeadlineFile(io.FileIO):
    """The archive file, with the deadline checked on every raw read. tarfile
    skips a member by reading through it, which for a compressed archive
    means inflating all of it -- one member can take arbitrarily long, and a
    check made only between members never gets a turn (review L3)."""

    def __init__(self, path, deadline: float):
        super().__init__(path, "rb")
        self._deadline = deadline

    def _check(self):
        if time.monotonic() > self._deadline:
            raise _Unsafe(f"reading the archive took longer than {int(MAX_SECONDS)} seconds")

    def read(self, size=-1):
        self._check()
        return super().read(size)

    def readinto(self, buffer):
        self._check()
        return super().readinto(buffer)

    def readall(self):
        self._check()
        return super().readall()


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
    """Same shape as _plan_zip. Reading the next header means reading past
    the current member's data, so the declared total is checked here, before
    moving on, and the reads themselves are under the deadline (_DeadlineFile)."""
    plan, skipped, seen, declared = [], 0, 0, 0
    for member in archive:
        seen += 1
        if seen > MAX_MEMBERS * 2:
            raise _Unsafe("the archive has too many entries")
        if time.monotonic() > deadline:
            raise _Unsafe(f"reading the archive took longer than {int(MAX_SECONDS)} seconds")
        if member.isreg():
            declared += max(0, member.size)
            if declared > MAX_BYTES:
                raise _Unsafe(f"the archive expands to more than {fs_common.human_size(MAX_BYTES)}")
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


def _write_members(plan, open_member, dest: Path, deadline: float, quarantine: bytes = None) -> tuple:
    """(files written, bytes written, files the archive marked executable).

    Nothing is ever chmod-ed: a file gets the permissions open() gives a new
    file, which never include execute. With `quarantine`, every file and
    folder created is marked with it as it is made."""
    written = 0
    files = 0
    were_executable = 0
    root = os.path.realpath(dest)
    marked = set()

    def mark_folders(folder: Path):
        # Folders are created implicitly as well as from entries, so mark
        # every one between the file and the destination, once.
        while quarantine is not None and folder != dest and folder not in marked:
            write_quarantine(folder, quarantine)
            marked.add(folder)
            folder = folder.parent
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
            mark_folders(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        mark_folders(target.parent)
        source = open_member(handle)
        if source is None:
            continue
        # "xb": two entries with the same name must not overwrite each other.
        with source, open(target, "xb") as out:
            # Marked before its content is written, so there is no moment
            # at which the file is complete and unmarked.
            if quarantine is not None:
                write_quarantine(target, quarantine)
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
        # The archive's execute bit is counted, never applied (review C3).
        were_executable += 1 if executable else 0
        files += 1
    return files, written, were_executable


def extract_archive(archive: str, destination: str = None) -> dict:
    """Extract a .zip or .tar(.gz/.bz2/.xz) archive into a NEW folder.

    Safe by construction:
    * the destination folder is created by this call with an exclusive
      mkdir, so nothing that already exists is ever written into, merged
      with or replaced;
    * every entry name is validated before anything is written (zip-slip),
      and one bad entry refuses the whole archive;
    * links and special files are never created; files are written with
      plain permissions and never come out executable;
    * a downloaded archive's quarantine mark is carried onto everything
      extracted from it;
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
    raw = None
    created = False
    try:
        quarantine = read_quarantine(src.resolve())
        if zipfile.is_zipfile(src):
            opened = zipfile.ZipFile(src)
            plan, skipped = _plan_zip(opened)
            open_member = opened.open
        elif tarfile.is_tarfile(src):
            raw = io.BufferedReader(_DeadlineFile(src, deadline))
            opened = tarfile.open(fileobj=raw, mode="r:*")
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
        if quarantine is not None:
            write_quarantine(dest, quarantine)
        files, written, were_executable = _write_members(plan, open_member, dest, deadline, quarantine)
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
        if raw is not None:
            raw.close()

    top_level = sorted(os.listdir(dest))
    message = f"extracted {files} file{'' if files == 1 else 's'} from {src.name} into {dest}"
    if skipped:
        message += f"; left out {skipped} link{'' if skipped == 1 else 's'} or special file{'' if skipped == 1 else 's'}"
    if were_executable:
        message += (
            f"; {were_executable} of them {'was' if were_executable == 1 else 'were'} marked as runnable "
            "in the archive and came out as ordinary files"
        )
    if quarantine is not None:
        message += "; the contents are marked as downloaded, like the archive"
    return {
        "ok": True,
        "path": str(dest),
        "executable_bits_dropped": were_executable,
        "quarantined": quarantine is not None,
        "file_count": files,
        "size_bytes": written,
        "size": fs_common.human_size(written),
        "skipped": skipped,
        "top_level": top_level[:20],
        "message": message,
    }
