import json
import threading
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_FILE = LOG_DIR / "events.jsonl"

_write_lock = threading.Lock()


def log_event(event_type: str, **fields) -> None:
    """Append one structured record to the local JSON-lines event log.

    Intentionally just a file, no DB -- this is the raw material future
    skill-learning work reads over, not a queryable store in its own right.
    """
    record = {"ts": time.time(), "type": event_type, **fields}
    # default=str: a record must never fail to be written because one field
    # (an exception, a Path) isn't JSON -- that would turn logging a problem
    # into a second problem.
    line = json.dumps(record, default=str) + "\n"
    # Tool functions run on worker threads (dispatcher._in_worker) and may
    # log from them. One lock, one write per record: lines from different
    # threads can't interleave, and the file stays valid JSONL.
    with _write_lock:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(line)
