"""Shared machinery for the file/workspace skills. Not a skill itself.

Three things live here so that every file skill gets them the same way:

* StepLog -- the only way these skills touch the machine. Every step,
  including the read-only ones used for planning ("does that folder exist",
  "what names are taken"), goes through dispatch(), so preferences and the
  confirmation gate apply to a skill's steps exactly as they do to a direct
  call. No skill in this family imports a tool function or does its own
  filesystem I/O.
* Where "the Desktop" and "Downloads" are. One function, overridable, so
  the checks can point the whole family at a temp directory.
* move_into_folders() -- the one bulk-move routine behind organize_folder,
  organize_by_date, clean_desktop, collect_screenshots and
  archive_old_downloads. Its guarantees are structural: it only ever calls
  move_file (which refuses to overwrite) and create_folder, picks a free
  name instead of colliding, touches at most MAX_FILES_PER_RUN files, and
  with dry_run performs reads only.
"""

import os
from datetime import datetime
from pathlib import Path

# How many files one bulk run may move. A voice command is easy to say and
# hard to take back; a cap keeps one misheard sentence to a bounded, easily
# reversed change, and the result says when the cap was the reason it stopped.
MAX_FILES_PER_RUN = 200
# How many candidates one run looks at (find_files' own maximum).
MAX_FILES_CONSIDERED = 500

# Step records kept in a skill's result. The result is serialised into the
# model's context and the event log, so 200 move records can't ride along.
MAX_STEPS_REPORTED = 25
MAX_LIST_ITEMS_REPORTED = 5

# Set by the checks; LEUTHERIA_FILES_HOME does the same for a whole agent
# process (an end-to-end run that must not touch the real Desktop).
HOME_DIR = None


def home() -> Path:
    override = HOME_DIR or os.environ.get("LEUTHERIA_FILES_HOME")
    return Path(override).expanduser() if override else Path.home()


def desktop() -> Path:
    return home() / "Desktop"


def downloads() -> Path:
    return home() / "Downloads"


def documents() -> Path:
    return home() / "Documents"


def pictures() -> Path:
    return home() / "Pictures"


def now() -> datetime:
    """The clock, as a function so the checks can pin it."""
    return datetime.now()


def resolve(path, default: Path = None) -> Path:
    """An absolute path from a skill argument. `~` is expanded here, before
    it reaches any tool."""
    if path is None or str(path).strip() == "":
        return default
    return Path(os.path.abspath(Path(str(path).strip()).expanduser()))


def as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def as_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, result))


def plural(count: int, singular: str, plural_form: str = None) -> str:
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"


# --- file categories ------------------------------------------------------

# (folder name, spoken singular, spoken plural, extensions)
CATEGORIES = (
    ("Images", "image", "images", "jpg jpeg png gif heic heif webp tif tiff bmp svg raw cr2 nef dng psd ico"),
    ("Documents", "document", "documents", "pdf doc docx txt rtf md pages odt epub tex"),
    ("Spreadsheets", "spreadsheet", "spreadsheets", "xls xlsx csv tsv numbers ods"),
    ("Presentations", "presentation", "presentations", "ppt pptx key odp"),
    ("Audio", "audio file", "audio files", "mp3 wav m4a aac flac ogg aiff aif opus"),
    ("Video", "video", "videos", "mp4 mov mkv avi webm m4v wmv"),
    ("Archives", "archive", "archives", "zip tar gz tgz bz2 xz 7z rar"),
    ("Installers", "installer", "installers", "dmg pkg"),
    (
        "Code",
        "code file",
        "code files",
        "py js ts jsx tsx html css json sh c h cpp hpp java go rs swift rb php yaml yml xml sql ipynb toml",
    ),
)
OTHER = ("Other", "other file", "other files")
SCREENSHOTS = ("Screenshots", "screenshot", "screenshots")

_EXT_TO_CATEGORY = {f".{ext}": row[:3] for row in CATEGORIES for ext in row[3].split()}
_SPOKEN = {row[0]: (row[1], row[2]) for row in (*CATEGORIES, OTHER, SCREENSHOTS)}

# A browser is still writing these. Moving one mid-download breaks the
# download, so no bulk skill ever picks them up.
IN_PROGRESS_SUFFIXES = (".crdownload", ".download", ".part", ".partial", ".tmp")

SCREENSHOT_PREFIXES = ("screenshot", "screen shot", "screen recording", "cleanshot")
SCREENSHOT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".heic", ".tiff", ".pdf", ".mov", ".mp4", ".gif")


def extension_of(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def in_progress(name: str) -> bool:
    return name.lower().endswith(IN_PROGRESS_SUFFIXES)


def category_of(name: str):
    """(folder, singular, plural) for a known type, else None."""
    return _EXT_TO_CATEGORY.get(extension_of(name))


def is_screenshot(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith(SCREENSHOT_PREFIXES) and lowered.endswith(SCREENSHOT_EXTENSIONS)


def describe_category(key: str, count: int) -> str:
    singular, plural_form = _SPOKEN.get(key, (key, key))
    return plural(count, singular, plural_form)


def describe_month(key: str, count: int) -> str:
    return f"{count} from {key}"


def summarise(counts: dict, describe, most: int = 4) -> str:
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    parts = [describe(key, count) for key, count in ordered[:most]]
    rest = sum(count for _, count in ordered[most:])
    if rest:
        parts.append(f"{rest} more in other groups")
    return ", ".join(parts)


def unique_name(name: str, taken: set) -> str:
    """name, or "stem (2).ext", "stem (3).ext"... -- the first not in
    `taken`. Compared case-insensitively: the default macOS volume treats
    Report.pdf and report.pdf as the same file."""
    if name.casefold() not in taken:
        return name
    stem, ext = os.path.splitext(name)
    n = 2
    while f"{stem} ({n}){ext}".casefold() in taken:
        n += 1
    return f"{stem} ({n}){ext}"


# --- guard rails ------------------------------------------------------------

def refuse_to_reorganise(folder: Path):
    """Why a bulk skill won't rearrange this folder, or None if it may.
    These are places where moving files by type breaks things rather than
    tidies them.

    Decided by core/path_policy.py on the resolved path and file identity.
    The old string-prefix list here was walked around by "/users/<me>",
    "~/library/Preferences", a folder inside a .photoslibrary, /Volumes/X
    and any subfolder of a repository (review M1)."""
    from agent.core import path_policy

    return path_policy.bulk_refusal(folder)


async def check_reorganisable(log, folder: Path):
    """None if a bulk skill may rearrange `folder`, else the refusal result."""
    reason = refuse_to_reorganise(folder)
    if reason:
        return log.refuse(f"I won't reorganise {folder}: {reason}")
    # A repository's top-level files are where its tooling expects them.
    repo = await log.run("file_info", {"path": str(folder / ".git")})
    if failed(repo):
        return log.fail("file_info", repo, "checking the folder")
    if repo.get("exists"):
        return log.refuse(f"I won't reorganise {folder}: it is a code project, and moving its files would break it")
    return None


# --- steps ------------------------------------------------------------------


def _compact(value):
    """A step's args/result with long lists cut down, for the record only."""
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items()}
    if isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS_REPORTED:
            omitted = len(value) - MAX_LIST_ITEMS_REPORTED
            return [_compact(item) for item in value[:MAX_LIST_ITEMS_REPORTED]] + [f"... and {omitted} more"]
        return [_compact(item) for item in value]
    return value


class StepLog:
    """Runs a skill's steps through dispatch() and keeps a bounded record."""

    def __init__(self, websocket, pending):
        self.websocket = websocket
        self.pending = pending
        self.records = []
        self.total = 0

    async def run(self, tool: str, args: dict) -> dict:
        # Imported lazily: dispatcher imports the skills registry, which
        # imports the skill modules, which import this.
        from agent.core.dispatcher import dispatch

        result = await dispatch(tool, args, self.websocket, self.pending)
        if not isinstance(result, dict):
            result = {"ok": False, "error": f"{tool} returned no result"}
        self.total += 1
        record = {"tool": tool, "args": _compact(args), "result": _compact(result)}
        if len(self.records) < MAX_STEPS_REPORTED or result.get("ok") is False:
            self.records.append(record)
        return result

    def export(self) -> dict:
        exported = {"steps": list(self.records)}
        if self.total > len(self.records):
            exported["steps_total"] = self.total
        return exported

    def fail(self, tool: str, result: dict, doing: str = None, **extra) -> dict:
        """The skill's result when a step failed: says which step and why."""
        reason = (result or {}).get("error") or "no reason given"
        what = f"{tool} failed while {doing}" if doing else f"the {tool} step failed"
        return {"ok": False, "error": f"{what}: {reason}", "failed_step": tool, **extra, **self.export()}

    def refuse(self, error: str) -> dict:
        return {"ok": False, "error": error, **self.export()}

    def done(self, message: str, **fields) -> dict:
        return {"ok": True, "message": message, **fields, **self.export()}


def failed(result: dict) -> bool:
    return not isinstance(result, dict) or result.get("ok") is not True


MAX_NAME_ATTEMPTS = 20


async def free_path(log: StepLog, folder: Path, name: str):
    """A path in `folder` that nothing occupies: `name`, else "stem (2).ext"
    and so on, each checked with file_info. Returns (path, None) or
    (None, failure_result). The tool that then creates the path still
    refuses to overwrite -- this picks a name, it is not the guard."""
    stem, ext = os.path.splitext(name)
    for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
        candidate = folder / (name if attempt == 1 else f"{stem} ({attempt}){ext}")
        info = await log.run("file_info", {"path": str(candidate)})
        if failed(info):
            return None, log.fail("file_info", info, f"checking {candidate.name}")
        if not info.get("exists"):
            return candidate, None
    return None, log.refuse(f"{MAX_NAME_ATTEMPTS} files named like {name} already exist in {folder}")


def spoken_when(modified: str) -> str:
    """'2026-10-12 15:04' -> 'today at 3:04 PM' / 'yesterday at ...' / 'on October 3'."""
    try:
        moment = datetime.strptime(modified, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""
    days = (now().date() - moment.date()).days
    clock = moment.strftime("%I:%M %p").lstrip("0")
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"yesterday at {clock}"
    if 1 < days < 7:
        return f"{moment.strftime('%A')} at {clock}"
    return f"on {moment.strftime('%B')} {moment.day}"


# --- the bulk move ----------------------------------------------------------


async def list_candidates(log: StepLog, folder: Path, **filters):
    """Top-level, visible, regular files in `folder` via find_files.
    Returns (files, more_exist, error_result)."""
    args = {"path": str(folder), "recursive": False, "sort": "name", "limit": MAX_FILES_CONSIDERED, **filters}
    found = await log.run("find_files", args)
    if failed(found):
        return None, False, log.fail("find_files", found, f"looking in {folder.name or folder}")
    return found.get("files", []), bool(found.get("truncated") or found.get("incomplete")), None


async def move_into_folders(
    log: StepLog,
    files: list,
    classify,
    *,
    describe,
    into: str,
    dry_run: bool,
    more_exist: bool = False,
    nothing: str = "There was nothing to move.",
) -> dict:
    """Move each file to the folder `classify` picks for it.

    classify(file) -> (destination_folder: Path, group_key: str), or None to
    leave the file where it is. Files still downloading are always left.
    Never overwrites: a name already taken in the destination gets a
    " (2)" suffix instead. Stops at the first step that fails and reports
    which, with what had already been moved.
    """
    planned = []  # (file, destination_folder, group_key)
    left = 0
    for file in files:
        choice = None if in_progress(file["name"]) else classify(file)
        if choice is None:
            left += 1
        else:
            planned.append((file, Path(choice[0]), choice[1]))

    over_cap = max(0, len(planned) - MAX_FILES_PER_RUN)
    planned = planned[:MAX_FILES_PER_RUN]
    if not planned:
        message = nothing
        if left:
            message += f" I left {plural(left, 'file')} of other kinds where {'it was' if left == 1 else 'they were'}."
        return log.done(message, dry_run=dry_run, moved=0, by_group={}, left_in_place=left, remaining=0)

    # What names are already taken in each destination folder.
    taken = {}
    to_create = []
    for _, folder, _ in planned:
        if folder in taken:
            continue
        info = await log.run("file_info", {"path": str(folder)})
        if failed(info):
            return log.fail("file_info", info, f"checking {folder.name}")
        if not info.get("exists"):
            taken[folder] = set()
            to_create.append(folder)
        elif info.get("kind") != "folder":
            return log.refuse(f"{folder} exists but is not a folder, so nothing was moved")
        else:
            listing = await log.run("list_files", {"path": str(folder)})
            if failed(listing):
                return log.fail("list_files", listing, f"reading {folder.name}")
            taken[folder] = {name.casefold() for name in listing.get("files", [])}

    moves = []  # (source, destination, group_key, renamed)
    for file, folder, key in planned:
        name = unique_name(file["name"], taken[folder])
        taken[folder].add(name.casefold())
        moves.append((file["path"], str(folder / name), key, name != file["name"]))

    counts = {}
    renamed = 0
    moved = 0

    def tally(key, was_renamed):
        nonlocal renamed, moved
        counts[key] = counts.get(key, 0) + 1
        renamed += 1 if was_renamed else 0
        moved += 1

    def fields():
        return {
            "dry_run": dry_run,
            "moved": 0 if dry_run else moved,
            "by_group": dict(counts),
            "renamed": renamed,
            "left_in_place": left,
            "remaining": over_cap,
            "more_exist": more_exist,
            "limit_per_run": MAX_FILES_PER_RUN,
            "examples": [f"{os.path.basename(src)} -> {dst}" for src, dst, _, _ in moves[:MAX_LIST_ITEMS_REPORTED]],
        }

    if not dry_run:
        for folder in to_create:
            made = await log.run("create_folder", {"path": str(folder)})
            if failed(made):
                return log.fail("create_folder", made, f"creating {folder.name}", **fields())
        for source, destination, key, was_renamed in moves:
            result = await log.run("move_file", {"source": source, "destination": destination})
            if failed(result):
                return log.fail(
                    "move_file",
                    result,
                    f"moving {os.path.basename(source)} (stopped there, after moving {plural(moved, 'file')})",
                    **fields(),
                )
            tally(key, was_renamed)
    else:
        for _, _, key, was_renamed in moves:
            tally(key, was_renamed)

    count = len(moves)
    summary = summarise(counts, describe)
    if dry_run:
        message = f"Dry run: I would move {plural(count, 'file')} {into}: {summary}. Nothing was moved."
    else:
        message = f"Moved {plural(count, 'file')} {into}: {summary}."
    if renamed:
        verb = "would be" if dry_run else ("was" if renamed == 1 else "were")
        message += f" {renamed} {verb} renamed so nothing was replaced."
    if left:
        message += f" I left {plural(left, 'file')} of other kinds where {'it was' if left == 1 else 'they were'}."
    if over_cap:
        waiting = "more are" if more_exist else f"{over_cap} more {'is' if over_cap == 1 else 'are'}"
        message += f" My limit is {MAX_FILES_PER_RUN} files per run and {waiting} waiting, so ask again to continue."
    elif more_exist:
        message += f" I only looked at the first {MAX_FILES_CONSIDERED} files there, so ask again to continue."
    result = fields()
    if dry_run:
        result["would_move"] = count
    return log.done(message, **result)
