import os
import subprocess
from pathlib import Path

from agent.tools import fs_common

MAX_LIMIT = 50
TIMEOUT_SECONDS = 8
MAX_QUERY_CHARS = 200
MAX_LINES_READ = 5000


def spotlight_search(query: str, folder: str = None, limit: int = 20) -> dict:
    """Find files by name anywhere on the Mac using the Spotlight index.
    Read-only. The query is one argv element after a fixed "-name" flag --
    never a shell string -- so nothing in it can be read as an option or a
    command, and the call has a timeout."""
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "query must be a non-empty file name or part of one"}
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS or "\x00" in query:
        return {"ok": False, "error": f"query must be at most {MAX_QUERY_CHARS} characters"}
    limit = fs_common.clamp_limit(limit, 20, MAX_LIMIT)

    argv = ["/usr/bin/mdfind"]
    if folder:
        scope = Path(folder).expanduser()
        if not scope.is_dir():
            return {"ok": False, "error": f"{scope} is not a folder"}
        argv += ["-onlyin", str(scope)]
    argv += ["-name", query]

    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, shell=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Spotlight did not answer within {TIMEOUT_SECONDS} seconds"}
    except OSError as e:
        return {"ok": False, "error": f"Spotlight is not available: {e}"}
    if completed.returncode != 0:
        return {"ok": False, "error": (completed.stderr or "Spotlight search failed").strip()[:300]}

    library = str(Path.home() / "Library") + os.sep
    candidates = []
    for line in completed.stdout.splitlines()[:MAX_LINES_READ]:
        if not line.startswith("/") or line.startswith(library):
            continue
        # Hidden folders and the system's own tree are index noise for a
        # request like "find my resume".
        if any(part.startswith(".") for part in line.split(os.sep)) or line.startswith("/System/"):
            continue
        candidates.append(line)

    files = []
    for line in candidates:
        try:
            st = os.lstat(line)
        except OSError:
            continue  # the index can be staler than the disk
        files.append((line, st))
    files.sort(key=lambda item: -item[1].st_mtime)

    results = [
        {
            "path": line,
            "name": os.path.basename(line),
            "kind": "folder" if os.path.isdir(line) else "file",
            "modified": fs_common.iso_minutes(st.st_mtime),
        }
        for line, st in files[:limit]
    ]
    total = len(files)
    message = f"Spotlight found {total} item{'' if total == 1 else 's'} named like {query!r}"
    if total > limit:
        message += f", showing the {limit} most recently modified"
    return {
        "ok": True,
        "files": results,
        "count": len(results),
        "total_matched": total,
        "truncated": total > limit,
        "message": message,
    }
