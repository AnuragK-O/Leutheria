import json
import os
import threading
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_FILE = LOG_DIR / "events.jsonl"

# Rotation: once events.jsonl passes MAX_BYTES it becomes events.jsonl.1
# (and .1 becomes .2 ...), keeping KEEP old files. Readers go through
# iter_records(), which reads the old files first, so usage counts, the
# Activity history and skill learning's repeat detection span the rotation.
MAX_BYTES = 5_000_000
KEEP = 4

_write_lock = threading.Lock()

# --- what is never written --------------------------------------------------
#
# The log is the raw material for the Library's counts, the Activity view
# and skill learning -- none of which reads what a tool *returned* beyond
# "ok". It used to keep file contents, clipboard text, fetched pages and
# command output verbatim, forever (review M5). Now:
#
#   * a value under one of CONTENT_KEYS, anywhere in a record (a tool's
#     result, a skill's steps, a model's tool-call arguments), is replaced
#     by its size: '[1234 chars not logged]';
#   * any other string longer than MAX_STRING is cut to its first HEAD
#     characters plus its length;
#   * the event types in KEEP_TEXT keep their top-level "text" (what the
#     user said), capped the same way.
#
# Paths, names, counts, sizes, ok/error and tool messages stay: that is what
# the readers use, and what makes the log useful for working out what
# happened.
CONTENT_KEYS = frozenset(
    {
        "content", "text", "stdout", "stderr", "output", "body", "html", "page_text", "clipboard",
        "readme_excerpt", "excerpt", "matches", "lines", "elements", "value", "selected_text",
    }
)
MAX_STRING = 200
HEAD = 80
KEEP_TEXT = frozenset({"stt_transcript", "llm_turn"})


def _cap(text: str) -> str:
    if len(text) <= MAX_STRING:
        return text
    return f"{text[:HEAD]}...[{len(text)} chars]"


def _gone(value) -> str:
    if isinstance(value, str):
        return f"[{len(value)} chars not logged]"
    if isinstance(value, (list, tuple)):
        return f"[{len(value)} items not logged]"
    if isinstance(value, dict):
        return f"[{len(value)} fields not logged]"
    return "[not logged]"


def scrub(value, depth: int = 0):
    """A copy of `value` with content removed and long strings capped."""
    if depth > 12:
        return "[nested too deep]"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in CONTENT_KEYS and item not in (None, "", [], {}) and not isinstance(item, (bool, int, float)):
                out[key] = _gone(item)
            else:
                out[key] = scrub(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _cap(value)
    return value


def _record(event_type: str, fields: dict) -> dict:
    kept = {}
    if event_type in KEEP_TEXT and isinstance(fields.get("text"), str):
        kept["text"] = _cap(fields["text"])
    return {"ts": time.time(), "type": event_type, **scrub(fields), **kept}


def _rotate_if_needed() -> None:
    try:
        if LOG_FILE.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    for n in range(KEEP, 0, -1):
        older = LOG_FILE.with_name(f"{LOG_FILE.name}.{n}")
        if n == KEEP:
            try:
                older.unlink()
            except OSError:
                pass
        newer = LOG_FILE if n == 1 else LOG_FILE.with_name(f"{LOG_FILE.name}.{n - 1}")
        try:
            os.replace(newer, older)
        except OSError:
            pass


def log_event(event_type: str, **fields) -> None:
    """Append one structured record to the local JSON-lines event log.

    Intentionally just a file, no DB -- this is the raw material future
    skill-learning work reads over, not a queryable store in its own right.
    Content is scrubbed on the way in (see CONTENT_KEYS).
    """
    record = _record(event_type, fields)
    # default=str: a record must never fail to be written because one field
    # (an exception, a Path) isn't JSON -- that would turn logging a problem
    # into a second problem.
    line = json.dumps(record, default=str) + "\n"
    # Tool functions run on worker threads (dispatcher._in_worker) and may
    # log from them. One lock, one write per record: lines from different
    # threads can't interleave, and the file stays valid JSONL.
    with _write_lock:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(line)


def log_files() -> list:
    """Every log file that exists, oldest first."""
    files = [LOG_FILE.with_name(f"{LOG_FILE.name}.{n}") for n in range(KEEP, 0, -1)] + [LOG_FILE]
    return [path for path in files if path.exists()]


def iter_records():
    """Every parseable record, oldest first, across rotated files."""
    for path in log_files():
        try:
            with path.open() as f:
                for line in f:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
