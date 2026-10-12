import fnmatch
import time
from pathlib import Path

from agent.tools import fs_common

MAX_LIMIT = 500
SCAN_SECONDS = 10.0
SCAN_ENTRIES = 200_000

SORTS = ("modified", "oldest", "size", "name")


def find_files(
    path: str,
    name: str = None,
    extension=None,
    min_size_mb=None,
    modified_within_days=None,
    older_than_days=None,
    recursive: bool = True,
    sort: str = "modified",
    limit: int = 50,
    include_hidden: bool = False,
) -> dict:
    """Read-only search for regular files under a folder. Never follows
    symlinks; the scan is bounded in time and entries, the result in count."""
    root = Path(path).expanduser()
    if not root.is_dir():
        return {"ok": False, "error": f"{root} is not a folder"}
    if sort not in SORTS:
        return {"ok": False, "error": f"sort must be one of: {', '.join(SORTS)}"}

    min_mb, error = fs_common.number(min_size_mb, "min_size_mb")
    within, error2 = fs_common.number(modified_within_days, "modified_within_days")
    older, error3 = fs_common.number(older_than_days, "older_than_days")
    for problem in (error, error2, error3):
        if problem:
            return {"ok": False, "error": problem}

    limit = fs_common.clamp_limit(limit, 50, MAX_LIMIT)
    wanted_exts = fs_common.extensions(extension)
    pattern = (name or "").strip().lower()
    is_glob = any(ch in pattern for ch in "*?[")
    min_bytes = int(min_mb * 1_000_000) if min_mb is not None else None
    now = time.time()
    newer_than = now - within * 86400 if within is not None else None
    older_than = now - older * 86400 if older is not None else None

    budget = fs_common.Budget(SCAN_SECONDS, SCAN_ENTRIES)
    matches = []
    for file_path, file_name, st in fs_common.walk_files(
        root, budget, recursive=bool(recursive), include_hidden=bool(include_hidden)
    ):
        if wanted_exts and fs_common.suffix_of(file_name) not in wanted_exts:
            continue
        if pattern:
            lowered = file_name.lower()
            if is_glob:
                if not fnmatch.fnmatchcase(lowered, pattern):
                    continue
            elif pattern not in lowered:
                continue
        if min_bytes is not None and st.st_size < min_bytes:
            continue
        if newer_than is not None and st.st_mtime < newer_than:
            continue
        if older_than is not None and st.st_mtime > older_than:
            continue
        matches.append((file_path, file_name, st.st_size, st.st_mtime))

    if sort == "modified":
        matches.sort(key=lambda m: (-m[3], m[1].lower()))
    elif sort == "oldest":
        matches.sort(key=lambda m: (m[3], m[1].lower()))
    elif sort == "size":
        matches.sort(key=lambda m: (-m[2], m[1].lower()))
    else:
        matches.sort(key=lambda m: (m[1].lower(), m[0]))

    files = [
        {
            "path": file_path,
            "name": file_name,
            "size_bytes": size,
            "size": fs_common.human_size(size),
            "modified": fs_common.iso_minutes(mtime),
        }
        for file_path, file_name, size, mtime in matches[:limit]
    ]
    total = len(matches)
    message = f"found {total} matching file{'' if total == 1 else 's'} in {root}"
    if total > limit:
        message += f", showing the first {limit}"
    if budget.exhausted:
        message += "; the folder was too large to scan completely, so there may be more"
    return {
        "ok": True,
        "files": files,
        "count": len(files),
        "total_matched": total,
        "truncated": total > limit,
        "incomplete": budget.exhausted,
        "message": message,
    }
