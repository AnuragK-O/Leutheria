import stat
from pathlib import Path

MAX_CHARS = 20000  # cap so a huge file can't blow up the LLM context
# Read at most this many bytes. UTF-8 is at most 4 bytes a character, so
# this always covers MAX_CHARS, and a 10 GB file (or /dev/zero, which
# is_file() already refuses) costs 80 KB instead of all of it (review M3).
MAX_BYTES = MAX_CHARS * 4


def read_file(path: str) -> dict:
    target = Path(path).expanduser()
    try:
        st = target.stat()
    except OSError:
        return {"ok": False, "error": f"{target} is not a file"}
    if not stat.S_ISREG(st.st_mode):
        return {"ok": False, "error": f"{target} is not a file"}

    try:
        with target.open("rb") as f:
            data = f.read(MAX_BYTES + 1)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Newlines normalised as read_text() did before.
    text = data[:MAX_BYTES].decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    truncated = len(data) > MAX_BYTES or len(text) > MAX_CHARS
    return {"ok": True, "content": text[:MAX_CHARS], "truncated": truncated}
