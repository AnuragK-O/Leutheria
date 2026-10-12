"""Shared plumbing for the read-only file tools (find_files, folder_size,
find_duplicates, search_file_contents, zip_folder's pre-scan). Not a tool
itself -- nothing here is registered.

Two properties every caller relies on:

* The walk never follows a symlink, so it can't loop, can't leave the folder
  it was pointed at, and can't count the same tree twice.
* Every walk runs against a Budget (wall clock + entry count). dispatch()
  calls a tool function synchronously on the agent's one event loop, so an
  unbounded scan of a home folder would freeze confirmations and the voice
  session for as long as it took. A tool that runs out of budget returns
  what it has and says so ("incomplete": True) rather than running on.
"""

import os
import stat
import time
from datetime import datetime
from pathlib import Path

# Never worth descending into when *searching*: enormous, machine-generated,
# and never what someone asking for "that PDF" means.
PRUNE_DIRS = frozenset({"node_modules", "__pycache__"})

# macOS packages: folders that Finder shows as one file. Searching treats
# them the way Finder does -- opaque.
BUNDLE_SUFFIXES = (
    ".app",
    ".photoslibrary",
    ".musiclibrary",
    ".tvlibrary",
    ".framework",
    ".bundle",
    ".xcodeproj",
    ".xcworkspace",
    ".logicx",
    ".imovielibrary",
)


class Budget:
    """A wall-clock and entry-count allowance for one tool call."""

    def __init__(self, seconds: float, max_entries: int):
        self.deadline = time.monotonic() + seconds
        self.max_entries = max_entries
        self.entries = 0
        self.exhausted = False
        self.unreadable = 0  # folders skipped because the OS refused

    def spend(self) -> bool:
        """Count one entry. False once the allowance is gone."""
        if self.exhausted:
            return False
        self.entries += 1
        # The clock is only read every 256 entries; time.monotonic() per
        # file is measurable on a 100k-file tree.
        if self.entries > self.max_entries or (self.entries & 0xFF == 0 and time.monotonic() > self.deadline):
            self.exhausted = True
            return False
        return True

    def out_of_time(self) -> bool:
        if not self.exhausted and time.monotonic() > self.deadline:
            self.exhausted = True
        return self.exhausted


def is_hidden(name: str) -> bool:
    return name.startswith(".")


def is_bundle(name: str) -> bool:
    return name.lower().endswith(BUNDLE_SUFFIXES)


def walk_files(root: Path, budget: Budget, *, recursive: bool = True, include_hidden: bool = False, searching: bool = True):
    """Yield (path_str, name, stat_result) for every regular file under root.

    Symlinks are never followed and never yielded; sockets, devices and
    FIFOs are skipped. With searching=True hidden entries (unless
    include_hidden), PRUNE_DIRS and package bundles are skipped, which is
    right for "find me a file"; searching=False visits everything, which is
    right for "how big is this".
    """
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            budget.unreadable += 1
            continue
        for entry in entries:
            if not budget.spend():
                return
            name = entry.name
            if searching and not include_hidden and is_hidden(name):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if not recursive:
                    continue
                if searching and (name in PRUNE_DIRS or is_bundle(name)):
                    continue
                stack.append(entry.path)
            elif stat.S_ISREG(st.st_mode):
                yield entry.path, name, st


def human_size(num_bytes) -> str:
    """Decimal units, as Finder shows them: 1 GB is 1,000,000,000 bytes."""
    value = float(max(0, num_bytes or 0))
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            if unit == "bytes":
                return f"{int(value)} bytes"
            return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} TB"


def iso_minutes(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def number(value, name: str, minimum: float = 0.0):
    """Coerce an optional numeric argument. The model sometimes sends "30"
    for 30; a value that isn't a number at all is an error to report, not
    something to guess around. Returns (value_or_None, error_or_None)."""
    if value is None or value == "":
        return None, None
    if isinstance(value, bool):
        return None, f"{name} must be a number"
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None, f"{name} must be a number"
    if result != result or result in (float("inf"), float("-inf")):
        return None, f"{name} must be a number"
    if result < minimum:
        return None, f"{name} must be at least {minimum:g}"
    return result, None


def clamp_limit(value, default: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, result))


def extensions(value) -> frozenset:
    """'pdf', '.PDF', 'jpg, png' or ['jpg', 'png'] -> {'.pdf'} etc."""
    if not value:
        return frozenset()
    items = value if isinstance(value, (list, tuple, set)) else str(value).replace(",", " ").split()
    result = set()
    for item in items:
        text = str(item).strip().lower().lstrip("*")
        if text:
            result.add(text if text.startswith(".") else f".{text}")
    return frozenset(result)


def suffix_of(name: str) -> str:
    return os.path.splitext(name)[1].lower()
