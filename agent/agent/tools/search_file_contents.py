from pathlib import Path

from agent.core import path_policy
from agent.tools import fs_common

MAX_LIMIT = 100
MAX_FILE_BYTES = 2_000_000
MAX_MATCHES_PER_FILE = 3
MAX_LINE_CHARS = 200
SCAN_SECONDS = 10.0
SCAN_ENTRIES = 50_000
MAX_QUERY_CHARS = 200


def _matches_in(file_path: str, needle: str) -> list:
    try:
        with open(file_path, "rb") as f:
            data = f.read(MAX_FILE_BYTES + 1)
    except OSError:
        return []
    # A NUL in the first few KB is the usual cheap test for "not text";
    # without it every image and binary gets decoded and searched.
    if len(data) > MAX_FILE_BYTES or b"\x00" in data[:4096]:
        return []
    found = []
    for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
        if needle in line.lower():
            found.append({"line": number, "text": line.strip()[:MAX_LINE_CHARS]})
            if len(found) >= MAX_MATCHES_PER_FILE:
                break
    return found


def search_file_contents(path: str, text: str, extension=None, limit: int = 20) -> dict:
    """Find text files containing a phrase (case-insensitive, literal -- not
    a regular expression). Read-only and bounded: files over 2 MB and binary
    files are skipped, and the scan stops at its time or file allowance."""
    root = Path(path).expanduser()
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "text must be a non-empty phrase to search for"}
    if len(text) > MAX_QUERY_CHARS:
        return {"ok": False, "error": f"text must be at most {MAX_QUERY_CHARS} characters"}
    needle = text.strip().lower()
    limit = fs_common.clamp_limit(limit, 20, MAX_LIMIT)
    wanted_exts = fs_common.extensions(extension)

    budget = fs_common.Budget(SCAN_SECONDS, SCAN_ENTRIES)
    if root.is_file():
        candidates = [(str(root), root.name)]
    elif root.is_dir():
        # dispatch() checked the folder; the files under it are checked one
        # by one, so a search of ~ never quotes a line from ~/Library or a
        # key file (core/path_policy.py).
        allowed = path_policy.ReadFilter()
        candidates = (
            (p, n)
            for p, n, st in fs_common.walk_files(root, budget)
            if st.st_size <= MAX_FILE_BYTES and allowed.allows(p)
        )
    else:
        return {"ok": False, "error": f"{root} does not exist"}

    results = []
    searched = 0
    truncated = False
    for file_path, name in candidates:
        if wanted_exts and fs_common.suffix_of(name) not in wanted_exts:
            continue
        if budget.out_of_time():
            break
        searched += 1
        found = _matches_in(file_path, needle)
        if found:
            if len(results) >= limit:
                truncated = True
                break
            results.append({"path": file_path, "name": name, "matches": found})

    message = f"{len(results)} file{'' if len(results) == 1 else 's'} mention {text.strip()!r}"
    if truncated:
        message += f", showing the first {limit}"
    if budget.exhausted:
        message += "; the folder was too large to search completely"
    return {
        "ok": True,
        "files": results,
        "count": len(results),
        "files_searched": searched,
        "truncated": truncated,
        "incomplete": budget.exhausted,
        "message": message,
    }
