import os
import stat
from pathlib import Path

from agent.tools import fs_common

MAX_LIMIT = 50
SCAN_SECONDS = 15.0
SCAN_ENTRIES = 500_000


def folder_size(path: str, limit: int = 10) -> dict:
    """Total size of a folder and its largest immediate items. Read-only.
    Counts everything inside, hidden files included (a size that left them
    out would be wrong), never follows symlinks. Bounded: on a huge tree it
    returns a lower bound and says so."""
    root = Path(path).expanduser()
    if not root.is_dir():
        return {"ok": False, "error": f"{root} is not a folder"}
    limit = fs_common.clamp_limit(limit, 10, MAX_LIMIT)

    try:
        with os.scandir(root) as it:
            children = list(it)
    except OSError as e:
        return {"ok": False, "error": str(e)}

    budget = fs_common.Budget(SCAN_SECONDS, SCAN_ENTRIES)
    items = []
    file_count = 0
    for child in children:
        if not budget.spend():
            break
        try:
            st = child.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            size = 0
            for _, _, inner in fs_common.walk_files(Path(child.path), budget, searching=False):
                size += inner.st_size
                file_count += 1
            items.append((child.name, child.path, "folder", size))
        elif stat.S_ISREG(st.st_mode):
            file_count += 1
            items.append((child.name, child.path, "file", st.st_size))

    total = sum(item[3] for item in items)
    items.sort(key=lambda item: (-item[3], item[0].lower()))
    largest = [
        {"name": name, "path": item_path, "kind": kind, "size_bytes": size, "size": fs_common.human_size(size)}
        for name, item_path, kind, size in items[:limit]
    ]

    qualifier = "at least " if budget.exhausted else ""
    message = f"{root.name or str(root)} is {qualifier}{fs_common.human_size(total)} in {file_count} files"
    if largest:
        top = ", ".join(f"{item['name']} ({item['size']})" for item in largest[:3])
        message += f". Largest: {top}"
    if budget.exhausted:
        message += ". The folder was too large to measure completely"
    return {
        "ok": True,
        "path": str(root),
        "total_bytes": total,
        "total": fs_common.human_size(total),
        "file_count": file_count,
        "items": largest,
        "incomplete": budget.exhausted,
        "message": message,
    }
