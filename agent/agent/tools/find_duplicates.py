import hashlib
from collections import defaultdict
from pathlib import Path

from agent.tools import fs_common

MAX_LIMIT = 50
MAX_PATHS_PER_GROUP = 10
SCAN_SECONDS = 20.0
SCAN_ENTRIES = 100_000
MAX_HASH_BYTES = 2_000_000_000
HEAD_BYTES = 64 * 1024
CHUNK = 1024 * 1024


class _HashBudget:
    def __init__(self, budget: fs_common.Budget):
        self.budget = budget
        self.remaining = MAX_HASH_BYTES
        self.exhausted = False

    def take(self, count: int) -> bool:
        self.remaining -= count
        if self.remaining < 0 or self.budget.out_of_time():
            self.exhausted = True
        return not self.exhausted


def _digest(path: str, allowance: _HashBudget, head_only: bool):
    """Hex digest of the first HEAD_BYTES or of the whole file; None if the
    file can't be read or the allowance ran out part-way."""
    hasher = hashlib.blake2b(digest_size=20)
    try:
        with open(path, "rb") as f:
            if head_only:
                data = f.read(HEAD_BYTES)
                if not allowance.take(len(data)):
                    return None
                hasher.update(data)
            else:
                while True:
                    data = f.read(CHUNK)
                    if not data:
                        break
                    if not allowance.take(len(data)):
                        return None
                    hasher.update(data)
    except OSError:
        return None
    return hasher.hexdigest()


def find_duplicates(path: str, min_size_kb=1, recursive: bool = True, limit: int = 20) -> dict:
    """Report sets of files with identical content. Read-only: it reports,
    it never moves or removes anything.

    Files are compared by content, not name: same size, then the same first
    64 KB, then a hash of the whole file. Only the last step is evidence;
    the first two just avoid reading every file. Largest files are checked
    first, so if the time or byte allowance runs out the result is missing
    the least valuable sets, and says it is incomplete.
    """
    root = Path(path).expanduser()
    if not root.is_dir():
        return {"ok": False, "error": f"{root} is not a folder"}
    min_kb, error = fs_common.number(min_size_kb, "min_size_kb")
    if error:
        return {"ok": False, "error": error}
    # Empty files are all "identical" and never worth reporting.
    min_bytes = max(1, int((min_kb if min_kb is not None else 1) * 1000))
    limit = fs_common.clamp_limit(limit, 20, MAX_LIMIT)

    budget = fs_common.Budget(SCAN_SECONDS, SCAN_ENTRIES)
    by_size = defaultdict(list)
    scanned = 0
    for file_path, _, st in fs_common.walk_files(root, budget, recursive=bool(recursive)):
        if st.st_size >= min_bytes:
            by_size[st.st_size].append(file_path)
            scanned += 1

    allowance = _HashBudget(budget)
    groups = []
    for size in sorted((s for s, paths in by_size.items() if len(paths) > 1), reverse=True):
        if allowance.exhausted:
            break
        candidates = by_size[size]
        if size > HEAD_BYTES:
            by_head = defaultdict(list)
            for file_path in candidates:
                head = _digest(file_path, allowance, head_only=True)
                if head is not None:
                    by_head[head].append(file_path)
            clusters = [paths for paths in by_head.values() if len(paths) > 1]
        else:
            clusters = [candidates]
        for cluster in clusters:
            by_hash = defaultdict(list)
            for file_path in cluster:
                digest = _digest(file_path, allowance, head_only=False)
                if digest is not None:
                    by_hash[digest].append(file_path)
            for paths in by_hash.values():
                if len(paths) > 1:
                    groups.append((size, sorted(paths)))

    groups.sort(key=lambda g: (-g[0] * (len(g[1]) - 1), g[1][0]))
    wasted = sum(size * (len(paths) - 1) for size, paths in groups)
    incomplete = budget.exhausted or allowance.exhausted
    result_groups = [
        {
            "size_bytes": size,
            "size": fs_common.human_size(size),
            "count": len(paths),
            "wasted_bytes": size * (len(paths) - 1),
            "files": paths[:MAX_PATHS_PER_GROUP],
        }
        for size, paths in groups[:limit]
    ]

    if groups:
        message = (
            f"found {len(groups)} set{'' if len(groups) == 1 else 's'} of duplicate files in {root.name or root}, "
            f"{fs_common.human_size(wasted)} that could be freed"
        )
    else:
        message = f"no duplicate files found in {root.name or root}"
    if incomplete:
        message += "; the folder was too large to check completely, so there may be more"
    return {
        "ok": True,
        "path": str(root),
        "groups": result_groups,
        "duplicate_sets": len(groups),
        "wasted_bytes": wasted,
        "wasted": fs_common.human_size(wasted),
        "files_checked": scanned,
        "truncated": len(groups) > limit,
        "incomplete": incomplete,
        "message": message,
    }
