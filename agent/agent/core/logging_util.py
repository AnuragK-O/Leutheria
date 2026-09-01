import json
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_FILE = LOG_DIR / "events.jsonl"


def log_event(event_type: str, **fields) -> None:
    """Append one structured record to the local JSON-lines event log.

    Intentionally just a file, no DB -- this is the raw material future
    skill-learning work reads over, not a queryable store in its own right.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "type": event_type, **fields}
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")
