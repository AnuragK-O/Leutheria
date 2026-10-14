"""Check of the file/workspace tools and skills.

    cd agent
    ./.venv/bin/python scripts/skills_check_files.py

Every skill is driven through the real dispatch() with a fake websocket
that answers confirmations. Everything happens inside one temp directory:
HOME itself is pointed there (so even a stray "~" default cannot reach the
real Desktop, Downloads or Documents), files_common.HOME_DIR likewise, and
the event log, preferences and trust files are temp copies. take_screenshot
and Spotlight's subprocess are stubbed -- no screen capture, no mic, no
network, no port. Prints one line per check and exits non-zero if any fail.
"""

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

TMP = Path(os.path.realpath(tempfile.mkdtemp(prefix="leutheria_files_check_")))
HOME = TMP / "home"
for _name in ("Desktop", "Downloads", "Documents", "Pictures"):
    (HOME / _name).mkdir(parents=True)
REAL_HOME = Path.home()
os.environ["HOME"] = str(HOME)

from agent.core import logging_util  # noqa: E402

logging_util.LOG_DIR = TMP / "logs"
logging_util.LOG_FILE = logging_util.LOG_DIR / "events.jsonl"

from agent.core import preferences, trust  # noqa: E402

preferences.PREFS_FILE = TMP / "preferences.json"
preferences._overrides.clear()
trust.TRUST_FILE = TMP / "trusted_commands.json"

from agent.core import dispatcher  # noqa: E402
from agent.skills import files_common as common  # noqa: E402
from agent.skills import registry as skills_registry  # noqa: E402
from agent.skills.registry import SKILLS  # noqa: E402
from agent.tools import extract_archive as extract_mod  # noqa: E402
from agent.tools import spotlight_search as spotlight_mod  # noqa: E402
from agent.tools import zip_folder as zip_mod  # noqa: E402
from agent.tools.registry import TOOLS  # noqa: E402

skills_registry.GENERATED_DIR = TMP / "generated"
common.HOME_DIR = str(HOME)


async def _silent(*_args, **_kwargs):
    return None


dispatcher.speak = _silent  # confirmations must not play audio

FILE_TOOLS = [
    "find_files", "file_info", "folder_size", "zip_folder", "extract_archive",
    "find_duplicates", "write_new_file", "search_file_contents", "spotlight_search",
]
FILE_SKILLS = [
    "organize_folder", "organize_by_date", "clean_desktop", "collect_screenshots",
    "archive_old_downloads", "archive_folder", "project_snapshot", "find_large_files",
    "find_recent_files", "storage_report", "folder_summary", "daily_journal",
    "screenshot_to_folder", "duplicate_report", "extract_latest_download",
]

passed = 0
failures = []


def check(condition, label):
    global passed
    if condition:
        passed += 1
        print(f"ok    {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}")


class FakeSocket:
    """Answers every confirmation_required as soon as it is sent."""

    def __init__(self, pending, approve=True):
        self.pending = pending
        self.approve = approve
        self.asked = []

    async def send(self, raw):
        message = json.loads(raw)
        if message.get("type") == "confirmation_required":
            self.asked.append(message["tool"])
            self.pending[message["id"]].set_result({"approved": self.approve})


async def call(name, args, approve=True):
    pending = {}
    socket = FakeSocket(pending, approve)
    result = await dispatcher.dispatch(name, args, socket, pending)
    result_socket.append(socket)
    return result


result_socket = []


def put(path: Path, content="x", age_days=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    if age_days is not None:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    return path


def tree(root: Path) -> dict:
    """Every path under root -> content (or a marker), for before/after."""
    seen = {}
    for current, dirs, files in os.walk(root):
        for name in dirs:
            seen[os.path.relpath(os.path.join(current, name), root) + "/"] = None
        for name in files:
            full = os.path.join(current, name)
            seen[os.path.relpath(full, root)] = "link" if os.path.islink(full) else Path(full).read_bytes()
    return seen


def fresh(name: str) -> Path:
    path = TMP / "work" / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def reset_home(name: str) -> Path:
    path = HOME / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


# --------------------------------------------------------------------------
async def check_registry():
    for name in FILE_TOOLS:
        tool = TOOLS.get(name, {})
        check(tool.get("safety") == "safe" and callable(tool.get("fn")), f"registry: tool {name} registered as safe")
    for name in FILE_SKILLS:
        check(name in SKILLS and name not in TOOLS, f"registry: skill {name} registered")
    bad = []
    for name in FILE_TOOLS + FILE_SKILLS:
        entry = TOOLS.get(name) or SKILLS.get(name) or {}
        texts = [entry.get("description", "")]
        texts += [p.get("description", "") for p in entry.get("input_schema", {}).get("properties", {}).values()]
        for index, text in enumerate(texts):
            # A parameter description may show a literal wildcard; the
            # description proper is what the model echoes and TTS reads.
            banned = "`*#\\\n\t" if index == 0 else "`#\\\n\t"
            if not text or any(ch in text for ch in banned) or "--" in text:
                bad.append(name)
    check(not bad, f"registry: descriptions are plain prose, no markdown or escapes {sorted(set(bad))}")
    # Contiguous, not "last": other domains' blocks are appended after this one.
    names = list(TOOLS)
    start = names.index(FILE_TOOLS[0])
    check(names[start:start + len(FILE_TOOLS)] == FILE_TOOLS, "registry: file tools are one contiguous block in TOOLS")
    builtin = [n for n, s in SKILLS.items() if s.get("origin") != "generated"]
    start = builtin.index(FILE_SKILLS[0])
    check(builtin[start:start + len(FILE_SKILLS)] == FILE_SKILLS, "registry: file skills are one contiguous block in SKILLS")

    skills_dir = AGENT_DIR / "agent" / "skills"
    offenders = []
    for name in FILE_SKILLS + ["files_common"]:
        source = (skills_dir / f"{name}.py").read_text()
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("from agent.tools", "import agent.tools")) and not (
                "agent.tools.registry import TOOLS" in stripped or "agent.tools.fs_common import human_size" in stripped
            ):
                offenders.append(f"{name}: {stripped}")
            if any(word in stripped for word in ("shutil.", "os.rename", "os.remove", "os.unlink", ".unlink(", "open(", ".write_text(", ".mkdir(")):
                offenders.append(f"{name}: {stripped}")
    check(not offenders, f"skills: no skill imports a tool function or does its own file I/O {offenders}")


# --------------------------------------------------------------------------
async def check_write_new_file():
    work = fresh("write")
    target = work / "sub" / "deep" / "note.txt"
    result = await call("write_new_file", {"path": str(target), "content": "hello\nworld\n"})
    check(result.get("ok") is True and result.get("path") == str(target), "write_new_file: creates and returns {ok, path}")
    check(target.read_text() == "hello\nworld\n", "write_new_file: content written exactly, parents created")
    again = await call("write_new_file", {"path": str(target), "content": "REPLACED"})
    check(again.get("ok") is False and "already exists" in again.get("error", ""), "write_new_file: refuses an existing file")
    check(target.read_text() == "hello\nworld\n", "write_new_file: existing content untouched after refusal")
    folder = await call("write_new_file", {"path": str(work / "sub"), "content": "x"})
    check(folder.get("ok") is False, "write_new_file: refuses when the path is a folder")
    victim = put(work / "victim.txt", "precious")
    os.symlink(victim, work / "link.txt")
    linked = await call("write_new_file", {"path": str(work / "link.txt"), "content": "x"})
    check(linked.get("ok") is False and victim.read_text() == "precious", "write_new_file: refuses to write through a symlink")
    os.symlink(work / "nowhere.txt", work / "dangling.txt")
    dangling = await call("write_new_file", {"path": str(work / "dangling.txt"), "content": "x"})
    check(dangling.get("ok") is False and not (work / "nowhere.txt").exists(), "write_new_file: refuses a dangling symlink")
    uni = await call("write_new_file", {"path": str(work / "résumé ünï 日本.md"), "content": "ünï 日本"})
    check(uni.get("ok") and (work / "résumé ünï 日本.md").read_text(encoding="utf-8") == "ünï 日本", "write_new_file: unicode name and content")
    tilde = await call("write_new_file", {"path": "~/Documents/tilde.txt", "content": "t"})
    check(tilde.get("ok") and (HOME / "Documents" / "tilde.txt").exists(), "write_new_file: expands ~ (to the temp home)")
    empty = await call("write_new_file", {"path": str(work / "empty.txt"), "content": ""})
    check(empty.get("ok") and (work / "empty.txt").read_text() == "", "write_new_file: empty content allowed")
    missing = await call("write_new_file", {"path": str(work / "x.txt")})
    check(missing.get("ok") is False, "write_new_file: missing content is an error, not a crash")
    wrong = await call("write_new_file", {"path": str(work / "y.txt"), "content": 5})
    check(wrong.get("ok") is False and not (work / "y.txt").exists(), "write_new_file: non-text content refused")
    put(work / "afile", "f")
    under = await call("write_new_file", {"path": str(work / "afile" / "z.txt"), "content": "x"})
    check(under.get("ok") is False and "already exists" not in under.get("error", ""), "write_new_file: parent-is-a-file reported as such")


async def check_find_files():
    work = fresh("find")
    put(work / "Invoice March.pdf", "a" * 10)
    put(work / "invoice-april.PDF", "a" * 20)
    put(work / "photo.jpg", b"j" * 3_000_000)
    put(work / "old report.txt", "o", age_days=40)
    put(work / "sub" / "deep" / "nested invoice.pdf", "n")
    put(work / ".hidden.pdf", "h")
    put(work / ".git" / "config.pdf", "g")
    put(work / "node_modules" / "pkg" / "x.pdf", "m")
    put(work / "ünï cödé 文件.pdf", "u")
    outside = fresh("find_outside")
    put(outside / "secret invoice.pdf", "s")
    os.symlink(outside, work / "linkdir")
    os.symlink(outside / "secret invoice.pdf", work / "linkfile.pdf")

    result = await call("find_files", {"path": str(work), "name": "invoice"})
    names = sorted(f["name"] for f in result.get("files", []))
    check(names == ["Invoice March.pdf", "invoice-april.PDF", "nested invoice.pdf"], f"find_files: name substring, case-insensitive, recursive {names}")
    check("secret invoice.pdf" not in names, "find_files: never follows symlinks out of the folder")
    pdfs = await call("find_files", {"path": str(work), "extension": "pdf"})
    names = {f["name"] for f in pdfs["files"]}
    check(".hidden.pdf" not in names and "config.pdf" not in names and "x.pdf" not in names, "find_files: skips hidden files, .git and node_modules")
    check("ünï cödé 文件.pdf" in names and "invoice-april.PDF" in names, "find_files: extension is case-insensitive; unicode names")
    multi = await call("find_files", {"path": str(work), "extension": "jpg, .TXT"})
    check({f["name"] for f in multi["files"]} == {"photo.jpg", "old report.txt"}, "find_files: several extensions")
    glob = await call("find_files", {"path": str(work), "name": "inv*april*"})
    check([f["name"] for f in glob["files"]] == ["invoice-april.PDF"], "find_files: wildcard name")
    big = await call("find_files", {"path": str(work), "min_size_mb": 2})
    check([f["name"] for f in big["files"]] == ["photo.jpg"], "find_files: min_size_mb")
    recent = await call("find_files", {"path": str(work), "modified_within_days": 7})
    check("old report.txt" not in {f["name"] for f in recent["files"]} and recent["count"] >= 5, "find_files: modified_within_days")
    old = await call("find_files", {"path": str(work), "older_than_days": 30})
    check([f["name"] for f in old["files"]] == ["old report.txt"], "find_files: older_than_days")
    top = await call("find_files", {"path": str(work), "recursive": False})
    check("nested invoice.pdf" not in {f["name"] for f in top["files"]}, "find_files: recursive false stays at the top level")
    sized = await call("find_files", {"path": str(work), "sort": "size", "limit": 1})
    check(sized["files"][0]["name"] == "photo.jpg" and sized["truncated"] and sized["total_matched"] > 1, "find_files: sort by size, limit caps and says truncated")
    huge = await call("find_files", {"path": str(work), "limit": 999999})
    check(huge.get("ok") and huge["count"] <= 500, "find_files: limit clamped")
    check((await call("find_files", {"path": str(work / "nope")})).get("ok") is False, "find_files: missing folder is an error")
    check((await call("find_files", {"path": str(work), "sort": "random"})).get("ok") is False, "find_files: unknown sort rejected")
    check((await call("find_files", {"path": str(work), "min_size_mb": "big"})).get("ok") is False, "find_files: non-numeric size rejected")
    text = await call("find_files", {"path": str(work), "min_size_mb": "2"})
    check(text.get("ok") and text["count"] == 1, "find_files: numeric string accepted")
    empty = fresh("find_empty")
    none = await call("find_files", {"path": str(empty)})
    check(none.get("ok") and none["files"] == [] and "0 matching files" in none["message"], "find_files: empty folder")

    from agent.tools import find_files as ff

    saved = ff.SCAN_ENTRIES
    ff.SCAN_ENTRIES = 3
    capped = await call("find_files", {"path": str(work)})
    ff.SCAN_ENTRIES = saved
    check(capped.get("ok") and capped["incomplete"] is True and "may be more" in capped["message"], "find_files: scan budget stops the walk and says incomplete")


async def check_file_info():
    work = fresh("info")
    put(work / "a b.txt", "12345")
    put(work / "dir" / "one", "1")
    put(work / "dir" / "two", "2")
    os.symlink(work / "a b.txt", work / "ln")
    info = await call("file_info", {"path": str(work / "a b.txt")})
    check(info.get("exists") and info["kind"] == "file" and info["size_bytes"] == 5 and info["extension"] == ".txt", "file_info: file")
    folder = await call("file_info", {"path": str(work / "dir")})
    check(folder["kind"] == "folder" and folder["item_count"] == 2, "file_info: folder with item count")
    gone = await call("file_info", {"path": str(work / "missing")})
    check(gone.get("ok") is True and gone["exists"] is False, "file_info: missing path is exists false, not an error")
    link = await call("file_info", {"path": str(work / "ln")})
    check(link["kind"] == "symlink", "file_info: a symlink is reported as a link, not followed")


async def check_folder_size():
    work = fresh("size")
    put(work / "small.txt", "s" * 100)
    put(work / "big.bin", b"b" * 50_000)
    put(work / "sub" / "a.bin", b"a" * 200_000)
    put(work / "sub" / ".hidden", b"h" * 1000)
    (work / "emptydir").mkdir()
    os.symlink(REAL_HOME, work / "homelink")
    result = await call("folder_size", {"path": str(work), "limit": 2})
    check(result.get("total_bytes") == 251_100, f"folder_size: total counts everything incl. hidden, not links ({result.get('total_bytes')})")
    check([i["name"] for i in result["items"]] == ["sub", "big.bin"], "folder_size: largest items first, limit applied")
    check(result["file_count"] == 4 and result["incomplete"] is False, "folder_size: file count")
    empty = await call("folder_size", {"path": str(work / "emptydir")})
    check(empty.get("ok") and empty["total_bytes"] == 0 and empty["items"] == [], "folder_size: empty folder")
    check((await call("folder_size", {"path": str(work / "small.txt")})).get("ok") is False, "folder_size: a file is not a folder")


async def check_zip_folder():
    work = fresh("zip")
    source = work / "my project"
    put(source / "read me.txt", "hello")
    put(source / "src" / "ünï.py", "print(1)")
    put(source / "node_modules" / "junk.js", "junk")
    put(source / ".env", "SECRET")
    (source / "empty dir").mkdir()
    os.symlink(REAL_HOME, source / "homelink")
    put(source / ".DS_Store", "x")
    before = tree(source)

    result = await call("zip_folder", {"source": str(source)})
    archive = work / "my project.zip"
    check(result.get("ok") and result["path"] == str(archive) and archive.is_file(), "zip_folder: creates <name>.zip next to the source")
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        check(z.read("my project/src/ünï.py") == b"print(1)", "zip_folder: content round-trips, unicode and spaces")
    check("my project/empty dir/" in names and "my project/.env" in names, "zip_folder: keeps empty folders and hidden files")
    check(not any("homelink" in n or ".DS_Store" in n for n in names) and result["skipped_links"] == 1, "zip_folder: leaves out symlinks and .DS_Store")
    check(tree(source) == before, "zip_folder: source untouched")

    stamp = archive.read_bytes()
    again = await call("zip_folder", {"source": str(source)})
    check(again.get("ok") is False and "already exists" in again["error"] and archive.read_bytes() == stamp, "zip_folder: refuses to overwrite an existing zip")
    excluded = await call("zip_folder", {"source": str(source), "destination": str(work / "out" / "clean"), "exclude": ["node_modules", ".env"]})
    with zipfile.ZipFile(work / "out" / "clean.zip") as z:
        names = set(z.namelist())
    check(excluded.get("ok") and not any("node_modules" in n or ".env" in n for n in names), "zip_folder: exclude; .zip added; parent folders created")
    inside = await call("zip_folder", {"source": str(source), "destination": str(source / "self.zip")})
    check(inside.get("ok") is False and not (source / "self.zip").exists(), "zip_folder: refuses a destination inside the source")
    check((await call("zip_folder", {"source": str(work / "nope")})).get("ok") is False, "zip_folder: missing source")
    single = await call("zip_folder", {"source": str(source / "read me.txt"), "destination": str(work / "single.zip")})
    with zipfile.ZipFile(work / "single.zip") as z:
        check(single.get("ok") and z.namelist() == ["read me.txt"], "zip_folder: single file")
    (work / "hollow").mkdir()
    hollow = await call("zip_folder", {"source": str(work / "hollow")})
    check(hollow.get("ok") and hollow["file_count"] == 0, "zip_folder: empty folder")
    into_dir = await call("zip_folder", {"source": str(work / "hollow"), "destination": str(work / "out")})
    check(into_dir.get("ok") and (work / "out" / "hollow.zip").is_file(), "zip_folder: destination may be a folder")

    saved = zip_mod.MAX_FILES
    zip_mod.MAX_FILES = 2
    capped = await call("zip_folder", {"source": str(source), "destination": str(work / "capped.zip")})
    zip_mod.MAX_FILES = saved
    check(capped.get("ok") is False and "too large" in capped["error"] and not (work / "capped.zip").exists(), "zip_folder: file cap refuses and leaves no partial zip")


def make_zip(path: Path, members: dict, links: dict = None):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
        for name, target in (links or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            z.writestr(info, target)


async def check_extract_archive():
    work = fresh("extract")
    good = work / "bundle v1.zip"
    make_zip(good, {"docs/ünï note.txt": "hi", "a.txt": "A", "empty/": "", "__MACOSX/._a.txt": "junk", ".DS_Store": "x"})
    result = await call("extract_archive", {"archive": str(good)})
    dest = work / "bundle v1"
    check(result.get("ok") and result["path"] == str(dest) and result["file_count"] == 2, "extract_archive: zip into a new folder named after it")
    check((dest / "docs" / "ünï note.txt").read_text() == "hi" and (dest / "empty").is_dir(), "extract_archive: contents, unicode names, empty folders")
    check(not (dest / "__MACOSX").exists() and not (dest / ".DS_Store").exists(), "extract_archive: Finder sidecar entries ignored")
    check(good.is_file(), "extract_archive: the archive is kept")
    before = tree(dest)
    again = await call("extract_archive", {"archive": str(good)})
    check(again.get("ok") is False and "already exists" in again["error"] and tree(dest) == before, "extract_archive: refuses an existing destination, changes nothing")
    put(work / "occupied", "file")
    onto = await call("extract_archive", {"archive": str(good), "destination": str(work / "occupied")})
    check(onto.get("ok") is False and (work / "occupied").read_text() == "file", "extract_archive: refuses a destination that is a file")

    tar_path = work / "pack.tar.gz"
    with tarfile.open(tar_path, "w:gz") as t:
        data = b"#!/bin/sh\necho hi\n"
        info = tarfile.TarInfo("pack/run.sh")
        info.size = len(data)
        info.mode = 0o4755
        t.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("pack/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc"
        t.addfile(link)
        hard = tarfile.TarInfo("pack/hard")
        hard.type = tarfile.LNKTYPE
        hard.linkname = "pack/run.sh"
        t.addfile(hard)
    tarred = await call("extract_archive", {"archive": str(tar_path)})
    out = work / "pack"
    check(tarred.get("ok") and (out / "pack" / "run.sh").is_file() and tarred["skipped"] == 2, "extract_archive: tar.gz; links never created")
    mode = (out / "pack" / "run.sh").stat().st_mode & 0o7777
    # Review C3: an archive does not get to decide that a file is runnable.
    check(mode & 0o7111 == 0 and tarred["executable_bits_dropped"] == 1 and not os.path.lexists(out / "pack" / "escape"),
          f"extract_archive: executable and setuid bits dropped ({oct(mode)})")

    hostile = {
        "dotdot": {"ok.txt": "fine", "../../evil.txt": "pwned"},
        "absolute": {"/tmp/leutheria_evil_abs.txt": "pwned"},
        "backslash": {"..\\..\\evil.txt": "pwned"},
        "nested": {"a/b/../../../evil.txt": "pwned"},
        "drive": {"C:/evil.txt": "pwned"},
    }
    for label, members in hostile.items():
        sandbox = fresh(f"slip_{label}") / "inner" / "deeper"
        sandbox.mkdir(parents=True)
        path = sandbox / "bad.zip"
        make_zip(path, members)
        outer_before = tree(sandbox.parent.parent)
        refused = await call("extract_archive", {"archive": str(path)})
        outer_after = tree(sandbox.parent.parent)
        check(
            refused.get("ok") is False and "refused" in refused.get("error", "") and outer_before == outer_after
            and not (sandbox / "bad").exists() and not Path("/tmp/leutheria_evil_abs.txt").exists(),
            f"extract_archive: zip-slip ({label}) refused, nothing written, no folder left behind",
        )

    slip_tar = work / "slip.tar"
    with tarfile.open(slip_tar, "w") as t:
        info = tarfile.TarInfo("../evil_tar.txt")
        info.size = 5
        t.addfile(info, io.BytesIO(b"pwned"))
    refused = await call("extract_archive", {"archive": str(slip_tar)})
    check(refused.get("ok") is False and not (work / "evil_tar.txt").exists() and not (work / "slip").exists(), "extract_archive: tar path traversal refused")

    linked = work / "linked.zip"
    make_zip(linked, {"real.txt": "r"}, links={"sneaky": "/etc/passwd"})
    with_link = await call("extract_archive", {"archive": str(linked)})
    check(with_link.get("ok") and with_link["skipped"] == 1 and not os.path.lexists(work / "linked" / "sneaky"), "extract_archive: zip symlink entry skipped, never created")

    dupes = work / "dupes.zip"
    with zipfile.ZipFile(dupes, "w") as z:
        z.writestr("same.txt", "one")
        with __import__("warnings").catch_warnings():
            __import__("warnings").simplefilter("ignore")
            z.writestr("same.txt", "two")
    clash = await call("extract_archive", {"archive": str(dupes)})
    check(clash.get("ok") is False and not (work / "dupes").exists(), "extract_archive: duplicate entry names fail cleanly, folder removed")

    bomb = work / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("zeros.bin", b"\0" * 5_000_000)
    saved = extract_mod.MAX_BYTES
    extract_mod.MAX_BYTES = 1_000_000
    boom = await call("extract_archive", {"archive": str(bomb)})
    extract_mod.MAX_BYTES = saved
    check(boom.get("ok") is False and "expands to more than" in boom["error"] and not (work / "bomb").exists(), "extract_archive: size cap refuses a zip bomb")
    saved = extract_mod.MAX_MEMBERS
    extract_mod.MAX_MEMBERS = 1
    many = await call("extract_archive", {"archive": str(good), "destination": str(work / "many")})
    extract_mod.MAX_MEMBERS = saved
    check(many.get("ok") is False and not (work / "many").exists(), "extract_archive: entry-count cap")

    put(work / "notes.zip", "this is not a zip")
    check((await call("extract_archive", {"archive": str(work / "notes.zip")})).get("ok") is False, "extract_archive: a non-archive is an error")
    check((await call("extract_archive", {"archive": str(work / "missing.zip")})).get("ok") is False, "extract_archive: missing archive")

    src = work / "round trip"
    put(src / "a" / "b.txt", "B")
    await call("zip_folder", {"source": str(src), "destination": str(work / "rt.zip")})
    back = await call("extract_archive", {"archive": str(work / "rt.zip"), "destination": str(work / "rt out")})
    check(back.get("ok") and (work / "rt out" / "round trip" / "a" / "b.txt").read_text() == "B", "extract_archive: round trip of zip_folder's output")


async def check_find_duplicates():
    work = fresh("dupes")
    blob = os.urandom(200_000)
    put(work / "movie.bin", blob)
    put(work / "sub" / "movie copy.bin", blob)
    put(work / "sub" / "movie third.bin", blob)
    put(work / "same size.bin", os.urandom(200_000))
    put(work / "same head.bin", blob[:150_000] + os.urandom(50_000))
    put(work / "small a.txt", "tiny content here" * 100)
    put(work / "small ünï b.txt", "tiny content here" * 100)
    put(work / "empty1", "")
    put(work / "empty2", "")
    put(work / ".hidden copy.bin", blob)
    before = tree(work)
    result = await call("find_duplicates", {"path": str(work)})
    groups = result.get("groups", [])
    check(result.get("ok") and result["duplicate_sets"] == 2, f"find_duplicates: two sets found ({result.get('duplicate_sets')})")
    check(groups and groups[0]["count"] == 3 and groups[0]["wasted_bytes"] == 400_000, "find_duplicates: largest waste first, wasted bytes right")
    flat = [p for g in groups for p in g["files"]]
    check(not any("same size" in p or "same head" in p or "empty" in p or ".hidden" in p for p in flat), "find_duplicates: same size / same first 64 KB / empty / hidden are not duplicates")
    check(tree(work) == before, "find_duplicates: report only, nothing changed")
    one = await call("find_duplicates", {"path": str(work), "limit": 1})
    check(len(one["groups"]) == 1 and one["truncated"] is True and one["duplicate_sets"] == 2, "find_duplicates: limit caps the list and says so")
    flat_only = await call("find_duplicates", {"path": str(work), "recursive": False})
    check(flat_only["duplicate_sets"] == 1, "find_duplicates: recursive false")
    clean = fresh("dupes_none")
    put(clean / "a", "one thing entirely")
    none = await call("find_duplicates", {"path": str(clean)})
    check(none.get("ok") and none["groups"] == [] and "no duplicate" in none["message"], "find_duplicates: none found")
    check((await call("find_duplicates", {"path": str(work / "nope")})).get("ok") is False, "find_duplicates: missing folder")


async def check_search_contents():
    work = fresh("search")
    put(work / "wifi.md", "Home network\nThe WiFi Password is hunter2\n")
    put(work / "other.txt", "nothing here")
    put(work / "sub" / "also.txt", "wifi password again")
    put(work / "image.png", b"\x89PNG\x00\x00wifi password")
    put(work / ".secret.txt", "wifi password")
    result = await call("search_file_contents", {"path": str(work), "text": "wifi PASSWORD"})
    names = sorted(f["name"] for f in result.get("files", []))
    check(names == ["also.txt", "wifi.md"], f"search_file_contents: case-insensitive, recursive, skips binary and hidden {names}")
    hit = [f for f in result["files"] if f["name"] == "wifi.md"][0]["matches"][0]
    check(hit["line"] == 2 and "hunter2" in hit["text"], "search_file_contents: returns the matching line and its number")
    only_md = await call("search_file_contents", {"path": str(work), "text": "wifi", "extension": "md"})
    check([f["name"] for f in only_md["files"]] == ["wifi.md"], "search_file_contents: extension filter")
    regex = await call("search_file_contents", {"path": str(work), "text": ".*"})
    check(regex.get("ok") and regex["count"] == 0, "search_file_contents: the phrase is literal, not a pattern")
    check((await call("search_file_contents", {"path": str(work), "text": "  "})).get("ok") is False, "search_file_contents: empty phrase rejected")
    single = await call("search_file_contents", {"path": str(work / "wifi.md"), "text": "home"})
    check(single.get("ok") and single["count"] == 1, "search_file_contents: a single file may be searched")
    check((await call("search_file_contents", {"path": str(work / "nope"), "text": "x"})).get("ok") is False, "search_file_contents: missing path")


async def check_spotlight():
    work = fresh("spot")
    put(work / "My Resume.pdf", "r", age_days=5)
    put(work / "resume old.docx", "r", age_days=50)
    seen = {}
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        lines = [
            str(work / "resume old.docx"), str(work / "My Resume.pdf"), str(work / "gone resume.txt"),
            str(Path.home() / "Library" / "Caches" / "resume.cache"), "/System/Library/resume.plist",
            str(work / ".git" / "resume"), "not a path",
        ]
        return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")

    spotlight_mod.subprocess.run = fake_run
    try:
        result = await call("spotlight_search", {"query": "resume; rm -rf ~", "limit": 5})
        check(seen["argv"] == ["/usr/bin/mdfind", "-name", "resume; rm -rf ~"], "spotlight_search: fixed argv, query is one argument")
        check(seen["kwargs"].get("shell") is False and seen["kwargs"].get("timeout"), "spotlight_search: no shell, has a timeout")
        check([f["name"] for f in result["files"]] == ["My Resume.pdf", "resume old.docx"], "spotlight_search: newest first; Library, system, hidden and vanished paths dropped")
        scoped = await call("spotlight_search", {"query": "resume", "folder": str(work)})
        check(seen["argv"][1:3] == ["-onlyin", str(work)] and scoped.get("ok"), "spotlight_search: folder scope")
        check((await call("spotlight_search", {"query": " "})).get("ok") is False, "spotlight_search: empty query rejected")
        check((await call("spotlight_search", {"query": "x", "folder": str(work / "nope")})).get("ok") is False, "spotlight_search: missing folder rejected")

        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

        spotlight_mod.subprocess.run = slow
        timed = await call("spotlight_search", {"query": "x"})
        check(timed.get("ok") is False and "did not answer" in timed["error"], "spotlight_search: timeout is an error result, not a hang")
    finally:
        spotlight_mod.subprocess.run = real_run


# --------------------------------------------------------------------------
def messy(folder: Path):
    put(folder / "holiday photo.JPG", "img1")
    put(folder / "ünï 写真.png", "img2")
    put(folder / "report.pdf", "loose report")
    put(folder / "notes.txt", "n")
    put(folder / "budget.xlsx", "b")
    put(folder / "song.mp3", "s")
    put(folder / "setup.dmg", "d")
    put(folder / "bundle.zip", "z")
    put(folder / "mystery.xyz", "?")
    put(folder / "no extension", "?")
    put(folder / ".hidden.png", "h")
    put(folder / "movie.mp4.crdownload", "partial")
    put(folder / "A Folder" / "inner.png", "inner")
    put(folder / "Documents" / "report.pdf", "EXISTING report")
    put(folder / "Documents" / "Report (2).pdf", "EXISTING second")
    os.symlink(folder / "notes.txt", folder / "link.txt")


async def check_organize_folder():
    work = fresh("organize")
    messy(work)
    before = tree(work)
    dry = await call("organize_folder", {"path": str(work), "dry_run": True})
    check(dry.get("ok") and dry["dry_run"] is True and dry["would_move"] == 8 and dry["moved"] == 0, f"organize_folder: dry run reports the plan ({dry.get('would_move')})")
    check(tree(work) == before, "organize_folder: dry run changes nothing on disk")
    check("Dry run" in dry["message"] and "Nothing was moved" in dry["message"], "organize_folder: dry run message says so")
    check(not any(s["tool"] in ("move_file", "create_folder") for s in dry["steps"]), "organize_folder: dry run dispatches reads only")

    real = await call("organize_folder", {"path": str(work)})
    check(real.get("ok") and real["moved"] == 8, f"organize_folder: moves the known types ({real.get('moved')}) -- {real.get('error')}")
    check(real["by_group"] == {"Images": 2, "Documents": 2, "Spreadsheets": 1, "Audio": 1, "Installers": 1, "Archives": 1}, f"organize_folder: per-category counts {real.get('by_group')}")
    check((work / "Images" / "holiday photo.JPG").read_text() == "img1" and (work / "Images" / "ünï 写真.png").exists(), "organize_folder: files land in type folders; spaces and unicode")
    check((work / "Documents" / "report.pdf").read_text() == "EXISTING report" and (work / "Documents" / "Report (2).pdf").read_text() == "EXISTING second", "organize_folder: existing files in the destination untouched")
    check((work / "Documents" / "report (3).pdf").read_text() == "loose report" and real["renamed"] == 1, "organize_folder: a name collision (case-insensitive) gets a free suffix, never overwrites")
    stayed = ["mystery.xyz", "no extension", ".hidden.png", "movie.mp4.crdownload", "A Folder/inner.png"]
    check(all((work / s).exists() for s in stayed) and os.path.islink(work / "link.txt"), "organize_folder: unknown types, hidden, in-progress downloads, folders and links stay put")
    check(real["left_in_place"] == 3 and "left 3 files" in real["message"], "organize_folder: says how many it left")
    check(len(real["message"]) < 400 and "2 images" in real["message"], f"organize_folder: short spoken summary ({len(real['message'])} chars)")
    after = tree(work)
    again = await call("organize_folder", {"path": str(work)})
    check(again.get("ok") and again["moved"] == 0 and tree(work) == after, "organize_folder: a second run is a no-op")
    all_bytes = sorted(v for v in before.values() if isinstance(v, bytes))
    check(sorted(v for v in after.values() if isinstance(v, bytes)) == all_bytes, "organize_folder: every file's content survives (nothing deleted or replaced)")

    cap_dir = fresh("organize_cap")
    for i in range(7):
        put(cap_dir / f"pic {i}.png", str(i))
    saved = common.MAX_FILES_PER_RUN
    common.MAX_FILES_PER_RUN = 3
    try:
        capped = await call("organize_folder", {"path": str(cap_dir)})
        check(capped.get("ok") and capped["moved"] == 3 and capped["remaining"] == 4, "organize_folder: per-run cap respected")
        check("limit is 3 files per run" in capped["message"] and "4 more are waiting" in capped["message"], "organize_folder: the cap is stated in the message")
        check(len(list((cap_dir / "Images").iterdir())) == 3 and len([p for p in cap_dir.iterdir() if p.is_file()]) == 4, "organize_folder: exactly the cap moved on disk")
    finally:
        common.MAX_FILES_PER_RUN = saved
    many = fresh("organize_many")
    for i in range(40):
        put(many / f"f{i}.txt", str(i))
    bulk = await call("organize_folder", {"path": str(many)})
    check(bulk.get("ok") and bulk["moved"] == 40 and len(bulk["steps"]) <= common.MAX_STEPS_REPORTED and bulk["steps_total"] > 40, "organize_folder: step record is bounded, total reported")
    check(len(json.dumps(bulk)) < 20_000, "organize_folder: result stays small enough for the model's context")

    repo = fresh("organize_repo")
    put(repo / ".git" / "HEAD", "ref")
    put(repo / "logo.png", "l")
    refused = await call("organize_folder", {"path": str(repo)})
    check(refused.get("ok") is False and "code project" in refused["error"] and (repo / "logo.png").exists(), "organize_folder: refuses a git repository")
    for bad, label in ((str(HOME), "the home folder"), ("/", "the disk root"), ("/Applications", "a system folder"), (str(Path.home() / "Library" / "Preferences"), "the Library")):
        res = await call("organize_folder", {"path": bad, "dry_run": True})
        check(res.get("ok") is False and "won't reorganise" in res["error"], f"organize_folder: refuses {label}")
    check((await call("organize_folder", {"path": str(work / "nope")})).get("ok") is False, "organize_folder: missing folder fails, names the step")
    empty = fresh("organize_empty")
    nothing = await call("organize_folder", {"path": str(empty)})
    check(nothing.get("ok") and nothing["moved"] == 0 and list(empty.iterdir()) == [], "organize_folder: empty folder is a clean no-op, creates nothing")
    blocked = fresh("organize_blocked")
    put(blocked / "a.png", "a")
    put(blocked / "Images", "I am a file called Images")
    clash = await call("organize_folder", {"path": str(blocked)})
    check(clash.get("ok") is False and "not a folder" in clash["error"] and (blocked / "a.png").exists() and (blocked / "Images").read_text().startswith("I am"), "organize_folder: a file where the type folder should be stops it, nothing moved")
    check((await call("organize_folder", {})).get("ok") is False, "organize_folder: missing path argument is an error result")


async def check_gating():
    work = fresh("gating")
    put(work / "a.png", "a")
    put(work / "b.png", "b")
    preferences.set_preference("move_file", enabled=False)
    try:
        off = await call("organize_folder", {"path": str(work)})
        check(off.get("ok") is False and off.get("failed_step") == "move_file" and "disabled" in off["error"], "dispatch: a disabled tool stops the skill, which names the step")
        check((work / "a.png").exists() and (work / "b.png").exists(), "dispatch: nothing moved when move_file is disabled")
    finally:
        preferences.clear("move_file")

    preferences.set_preference("move_file", requires_confirmation=True)
    try:
        result_socket.clear()
        asked = await call("organize_folder", {"path": str(work)})
        check(asked.get("ok") and result_socket[-1].asked == ["move_file", "move_file"], "dispatch: each skill step goes through the confirmation gate (auto-approved)")
        work2 = fresh("gating2")
        put(work2 / "a.png", "a")
        put(work2 / "b.png", "b")
        declined = await call("organize_folder", {"path": str(work2)}, approve=False)
        check(declined.get("ok") is False and "declined" in declined["error"] and result_socket[-1].asked == ["move_file"], "dispatch: a declined step stops the skill at that step")
        check((work2 / "a.png").exists() and (work2 / "b.png").exists(), "dispatch: nothing moved after a decline")
    finally:
        preferences.clear("move_file")

    work3 = fresh("gating3")
    for name in ("a.png", "b.png", "c.png"):
        put(work3 / name, name)
    real = TOOLS["move_file"]["fn"]
    calls = {"n": 0}

    def flaky(source, destination):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"ok": False, "error": "disk said no"}
        return real(source, destination)

    TOOLS["move_file"]["fn"] = flaky
    try:
        partial = await call("organize_folder", {"path": str(work3)})
    finally:
        TOOLS["move_file"]["fn"] = real
    check(partial.get("ok") is False and partial["failed_step"] == "move_file" and "disk said no" in partial["error"] and "after moving 1 file" in partial["error"], "skills: stop on the first failed step and say which and how far it got")
    check(calls["n"] == 2 and (work3 / "c.png").exists() and len(list((work3 / "Images").iterdir())) == 1, "skills: no further steps run after a failure")


async def check_organize_by_date():
    work = fresh("bydate")
    put(work / "old.txt", "o", age_days=400)
    put(work / "older.png", "p", age_days=400)
    put(work / "new one.txt", "n")
    put(work / "Sub" / "x.txt", "x")
    month_old = datetime.fromtimestamp(time.time() - 400 * 86400).strftime("%Y-%m")
    month_new = datetime.now().strftime("%Y-%m")
    before = tree(work)
    dry = await call("organize_by_date", {"path": str(work), "dry_run": True})
    check(dry.get("ok") and dry["would_move"] == 3 and tree(work) == before, "organize_by_date: dry run plans, changes nothing")
    real = await call("organize_by_date", {"path": str(work)})
    check(real.get("ok") and real["by_group"] == {month_old: 2, month_new: 1}, f"organize_by_date: grouped by month modified {real.get('by_group')}")
    check((work / month_old / "old.txt").exists() and (work / month_new / "new one.txt").exists() and (work / "Sub" / "x.txt").exists(), "organize_by_date: files in month folders, subfolders untouched")
    check(f"2 from {month_old}" in real["message"], "organize_by_date: spoken per-month summary")
    repo = fresh("bydate_repo")
    put(repo / ".git" / "HEAD", "r")
    check((await call("organize_by_date", {"path": str(repo)})).get("ok") is False, "organize_by_date: refuses a git repository")


async def check_clean_desktop():
    desktop = reset_home("Desktop")
    put(desktop / "Screenshot 2026-10-12 at 9.41.07\u202fAM.png", "shot")
    put(desktop / "Screen Recording 2026-10-11 at 10.00.00.mov", "rec")
    put(desktop / "essay final.docx", "e")
    put(desktop / "weird.blob", "w")
    put(desktop / "pic.jpeg", "p")
    put(desktop / ".DS_Store", "ds")
    put(desktop / "Work Folder" / "keep.txt", "k")
    put(desktop / "dl.zip.download", "partial")
    before = tree(desktop)
    dry = await call("clean_desktop", {"dry_run": True})
    check(dry.get("ok") and dry["would_move"] == 5 and tree(desktop) == before, "clean_desktop: dry run on the (temp) Desktop changes nothing")
    real = await call("clean_desktop", {})
    cleanup = desktop / "Desktop Cleanup"
    check(real.get("ok") and real["moved"] == 5 and real["destination"] == str(cleanup), f"clean_desktop: moves loose files into Desktop Cleanup -- {real.get('error')}")
    check(real["by_group"] == {"Screenshots": 2, "Documents": 1, "Other": 1, "Images": 1}, f"clean_desktop: screenshots and unknown types get their own groups {real.get('by_group')}")
    left = sorted(p.name for p in desktop.iterdir())
    check(left == [".DS_Store", "Desktop Cleanup", "Work Folder", "dl.zip.download"], f"clean_desktop: folders, hidden and in-progress files stay {left}")
    check((cleanup / "Other" / "weird.blob").read_text() == "w" and (desktop / "Work Folder" / "keep.txt").exists(), "clean_desktop: contents intact")
    put(desktop / "pic.jpeg", "second pic")
    second = await call("clean_desktop", {})
    check(second.get("ok") and (cleanup / "Images" / "pic.jpeg").read_text() == "p" and (cleanup / "Images" / "pic (2).jpeg").read_text() == "second pic", "clean_desktop: a later run never overwrites an earlier one's files")
    third = await call("clean_desktop", {})
    check(third.get("ok") and third["moved"] == 0 and "no loose files" in third["message"], "clean_desktop: clean desktop is a no-op")
    check(not (REAL_HOME / "Desktop" / "Desktop Cleanup" / "Other" / "weird.blob").exists(), "clean_desktop: the real Desktop was not touched")
    shutil.rmtree(desktop)
    gone = await call("clean_desktop", {})
    check(gone.get("ok") is False and gone.get("failed_step") == "find_files", "clean_desktop: missing Desktop fails cleanly")
    desktop.mkdir()


async def check_collect_screenshots():
    desktop = reset_home("Desktop")
    reset_home("Pictures")
    put(desktop / "Screenshot 2026-10-01 at 08.00.00.png", "one")
    put(desktop / "Screen Shot 2019-01-01 at 1.00.00 PM.png", "two")
    put(desktop / "screenshot notes.txt", "not an image")
    put(desktop / "holiday.png", "h")
    target = HOME / "Pictures" / "Screenshots"
    put(target / "Screenshot 2026-10-01 at 08.00.00.png", "ALREADY THERE")
    before = tree(HOME)
    dry = await call("collect_screenshots", {"dry_run": True})
    check(dry.get("ok") and dry["would_move"] == 2 and tree(HOME) == before, "collect_screenshots: dry run")
    real = await call("collect_screenshots", {})
    check(real.get("ok") and real["moved"] == 2 and real["renamed"] == 1, f"collect_screenshots: moves only screenshots -- {real.get('error')}")
    check((target / "Screenshot 2026-10-01 at 08.00.00.png").read_text() == "ALREADY THERE" and (target / "Screenshot 2026-10-01 at 08.00.00 (2).png").read_text() == "one", "collect_screenshots: same-named screenshot kept beside the existing one")
    check((desktop / "holiday.png").exists() and (desktop / "screenshot notes.txt").exists(), "collect_screenshots: other files stay")
    none = await call("collect_screenshots", {})
    check(none.get("ok") and none["moved"] == 0 and "no screenshots" in none["message"], "collect_screenshots: nothing to collect")
    same = await call("collect_screenshots", {"source": str(target), "folder": str(target)})
    check(same.get("ok") is False, "collect_screenshots: source and target must differ")


async def check_archive_old_downloads():
    downloads = reset_home("Downloads")
    put(downloads / "ancient installer.dmg", "a", age_days=200)
    put(downloads / "old paper.pdf", "o", age_days=45)
    put(downloads / "last week.pdf", "l", age_days=7)
    put(downloads / "today.zip", "t")
    put(downloads / "Old Folder" / "x.txt", "x", age_days=300)
    put(downloads / ".old hidden", "h", age_days=300)
    before = tree(downloads)
    dry = await call("archive_old_downloads", {"dry_run": True})
    check(dry.get("ok") and dry["would_move"] == 2 and tree(downloads) == before, "archive_old_downloads: dry run changes nothing")
    real = await call("archive_old_downloads", {})
    month = datetime.fromtimestamp(time.time() - 200 * 86400).strftime("%Y-%m")
    check(real.get("ok") and real["moved"] == 2 and real["days"] == 30, f"archive_old_downloads: moves files older than 30 days -- {real.get('error')}")
    check((downloads / "Old Downloads" / month / "ancient installer.dmg").read_text() == "a", "archive_old_downloads: grouped by month under Old Downloads")
    check((downloads / "last week.pdf").exists() and (downloads / "today.zip").exists() and (downloads / "Old Folder" / "x.txt").exists() and (downloads / ".old hidden").exists(), "archive_old_downloads: recent files, folders and hidden files stay")
    check("older than 30 days" in real["message"] and "not deleted" not in real["message"], "archive_old_downloads: message states the threshold")
    week = await call("archive_old_downloads", {"days": 5})
    check(week.get("ok") and week["moved"] == 1 and not (downloads / "last week.pdf").exists(), "archive_old_downloads: custom days")
    none = await call("archive_old_downloads", {"days": "not a number"})
    check(none.get("ok") and none["moved"] == 0 and none["days"] == 30, "archive_old_downloads: a bad days value falls back to 30, nothing left to move")
    total = sum(1 for v in tree(downloads).values() if isinstance(v, bytes))
    check(total == 6, "archive_old_downloads: no file was deleted")


async def check_archive_folder():
    work = fresh("archive")
    src = work / "Trip Photos"
    put(src / "a.jpg", "a")
    put(src / "day 2" / "b.jpg", "b")
    before = tree(src)
    first = await call("archive_folder", {"path": str(src)})
    check(first.get("ok") and first["path"] == str(work / "Trip Photos.zip") and first["file_count"] == 2, f"archive_folder: zips next to the folder -- {first.get('error')}")
    check([s["tool"] for s in first["steps"]] == ["file_info", "file_info", "zip_folder"], "archive_folder: steps are file_info, file_info, zip_folder")
    second = await call("archive_folder", {"path": str(src)})
    check(second.get("ok") and second["path"] == str(work / "Trip Photos (2).zip"), "archive_folder: picks a free name instead of overwriting")
    check(tree(src) == before and "untouched" in first["message"], "archive_folder: original untouched")
    elsewhere = await call("archive_folder", {"path": str(src), "destination_folder": str(work / "out")})
    check(elsewhere.get("ok") and (work / "out" / "Trip Photos.zip").is_file(), "archive_folder: destination_folder")
    check((await call("archive_folder", {"path": str(src / "a.jpg")})).get("ok") is False, "archive_folder: a file is refused")
    check((await call("archive_folder", {"path": str(work / "nope")})).get("ok") is False, "archive_folder: missing folder")
    check((await call("archive_folder", {"path": str(src), "destination_folder": str(src / "day 2")})).get("ok") is False, "archive_folder: destination inside the folder refused")


async def check_project_snapshot():
    work = fresh("snapshot")
    proj = work / "my app"
    put(proj / "main.py", "print('hi')")
    put(proj / "src" / "lib.py", "x = 1")
    put(proj / "node_modules" / "dep" / "index.js", "junk")
    put(proj / ".venv" / "bin" / "python", "junk")
    put(proj / ".git" / "HEAD", "ref")
    put(proj / ".gitignore", "node_modules")
    before = tree(proj)
    result = await call("project_snapshot", {"path": str(proj), "note": "before  the\nrefactor"})
    snaps = HOME / "Snapshots" / "my app"
    check(result.get("ok") and Path(result["path"]).parent == snaps and result["file_count"] == 3, f"project_snapshot: zip saved under Snapshots/<project> -- {result.get('error')}")
    with zipfile.ZipFile(result["path"]) as z:
        names = z.namelist()
    check("my app/src/lib.py" in names and "my app/.gitignore" in names and not any(x in n for n in names for x in ("node_modules", ".venv", ".git/")), "project_snapshot: source in, dependencies and .git out")
    logged = (snaps / "snapshots.log").read_text()
    check(Path(result["path"]).name in logged and "before the refactor" in logged and logged.count("\n") == 1, "project_snapshot: one log line with the note")
    check(tree(proj) == before, "project_snapshot: project untouched")
    with_git = await call("project_snapshot", {"path": str(proj), "include_git": True, "snapshots_dir": str(work / "snaps")})
    with zipfile.ZipFile(with_git["path"]) as z:
        check(with_git.get("ok") and "my app/.git/HEAD" in z.namelist(), "project_snapshot: include_git and snapshots_dir")
    check((await call("project_snapshot", {"path": str(proj), "snapshots_dir": str(proj)})).get("ok") is False, "project_snapshot: refuses to store snapshots inside the project")
    check((await call("project_snapshot", {"path": str(work / "nope")})).get("ok") is False, "project_snapshot: missing project")


async def check_reports():
    downloads = reset_home("Downloads")
    reset_home("Desktop")
    documents = reset_home("Documents")
    put(downloads / "huge movie.mkv", b"m" * 3_000_000)
    put(downloads / "sub" / "big data.csv", b"c" * 2_000_000)
    put(downloads / "small.txt", "s")
    before = tree(HOME)
    large = await call("find_large_files", {"min_size_mb": 1})
    check(large.get("ok") and [f["name"] for f in large["largest_files"]] == ["huge movie.mkv", "big data.csv"], f"find_large_files: Downloads by default, largest first, any depth -- {large.get('error')}")
    check("huge movie.mkv at 3.0 MB" in large["message"] and "Downloads holds 5.0 MB" in large["message"], f"find_large_files: spoken summary ({large.get('message')})")
    none = await call("find_large_files", {"path": str(downloads)})
    check(none.get("ok") and none["largest_files"] == [] and "No single file" in none["message"] and "huge movie.mkv" in none["message"], "find_large_files: nothing over the threshold still names the largest item")
    check((await call("find_large_files", {"path": str(HOME / "nope")})).get("ok") is False, "find_large_files: missing folder")

    put(downloads / "statement.pdf", "new")
    put(downloads / "older statement.pdf", "old", age_days=2)
    put(HOME / "Desktop" / "slides.key", "k", age_days=1)
    put(documents / "ancient.pdf", "a", age_days=90)
    recent = await call("find_recent_files", {"extension": "pdf"})
    check(recent.get("ok") and [f["name"] for f in recent["files"]] == ["statement.pdf", "older statement.pdf"], f"find_recent_files: newest first across folders, filtered by type -- {recent.get('error')}")
    check("statement.pdf in Downloads, from today at" in recent["message"], f"find_recent_files: says where and when ({recent.get('message')})")
    everything = await call("find_recent_files", {"days": 2})
    names = [f["name"] for f in everything["files"]]
    check("slides.key" in names and "ancient.pdf" not in names, "find_recent_files: searches Desktop too, respects days")
    shutil.rmtree(HOME / "Desktop")
    missing_ok = await call("find_recent_files", {})
    check(missing_ok.get("ok"), "find_recent_files: a missing default folder is skipped, not an error")
    (HOME / "Desktop").mkdir()
    nothing = await call("find_recent_files", {"name": "zzz-nothing"})
    check(nothing.get("ok") and nothing["files"] == [] and nothing["message"].startswith("No files named like zzz-nothing"), "find_recent_files: no matches is a clean answer")
    check((await call("find_recent_files", {"path": str(HOME / "nope")})).get("ok") is False, "find_recent_files: an explicit missing folder is an error")

    storage = await call("storage_report", {})
    check(storage.get("ok") and [f["name"] for f in storage["folders"]][0] == "Downloads" and "gigabytes free" in storage["message"], f"storage_report: disk plus home folders, biggest first -- {storage.get('error')}")
    check({f["name"] for f in storage["folders"]} == {"Desktop", "Documents", "Downloads", "Pictures"}, "storage_report: only folders that exist are measured")

    summary = await call("folder_summary", {"path": str(downloads)})
    check(summary.get("ok") and summary["loose_files"] == 4 and summary["folders"] == 1 and summary["newest"] == "statement.pdf", f"folder_summary: counts and newest -- {summary.get('error')} {summary.get('message')}")
    check("3 documents, 1 video" in summary["message"] and "1 folder" in summary["message"], "folder_summary: spoken per-kind summary")
    empty = fresh("summary_empty")
    check((await call("folder_summary", {"path": str(empty)}))["message"] == "summary_empty is empty.", "folder_summary: empty folder")
    check((await call("folder_summary", {"path": str(empty / "nope")})).get("ok") is False, "folder_summary: missing folder")
    after = tree(HOME)
    for key in ("Desktop/",):
        before.pop(key, None)
    added = {k for k in after if k not in before} - {"Downloads/statement.pdf", "Downloads/older statement.pdf", "Desktop/slides.key", "Documents/ancient.pdf", "Desktop/"}
    check(not added, f"report skills: read-only, created nothing {sorted(added)}")


async def check_daily_journal():
    documents = reset_home("Documents")
    pinned = datetime(2026, 10, 12, 9, 5)
    real_now = common.now
    common.now = lambda: pinned
    stub_calls = []
    # The real open_file (system pack) would open the journal in an app on the
    # user's screen. Take it out for the "tool absent" half of this test, put a
    # recording stub in for the second half, and restore it at the end.
    real_open_file = TOOLS.pop("open_file", None)
    try:
        first = await call("daily_journal", {})
        path = documents / "Journal" / "2026-10-12.md"
        check(first.get("ok") and first["created"] and path.read_text() == "# Monday, October 12, 2026\n\n", f"daily_journal: starts today's file with a heading -- {first.get('error')}")
        check("not open on screen" in first["message"] and first["opened"] is False, "daily_journal: without open_file, says the file is not open")
        second = await call("daily_journal", {"text": "  Shipped the\nfiles pack  "})
        check(second.get("ok") and second["created"] is False and path.read_text() == "# Monday, October 12, 2026\n\n- 09:05 Shipped the files pack\n", "daily_journal: appends a timestamped entry, existing content kept")
        check([s["tool"] for s in second["steps"]] == ["file_info", "append_text"], "daily_journal: an existing day is never rewritten (no write_new_file step)")
        third = await call("daily_journal", {})
        check(third.get("ok") and "already exists" in third["message"] and path.read_text().count("\n") == 3, "daily_journal: no text on an existing day adds nothing")
        custom = await call("daily_journal", {"text": "ünï entry", "journal_dir": str(documents / "My Diary")})
        check(custom.get("ok") and "ünï entry" in (documents / "My Diary" / "2026-10-12.md").read_text(encoding="utf-8"), "daily_journal: journal_dir, unicode entry, created with entry in one go")
        (documents / "Blocked").mkdir()
        (documents / "Blocked" / "2026-10-12.md").mkdir()
        blocked = await call("daily_journal", {"journal_dir": str(documents / "Blocked")})
        check(blocked.get("ok") is False, "daily_journal: a folder where the file should be is refused")

        def open_file(path):
            stub_calls.append(path)
            return {"ok": True}

        TOOLS["open_file"] = {"fn": open_file, "safety": "safe", "description": "stub", "input_schema": {"type": "object", "properties": {}}}
        opened = await call("daily_journal", {"text": "with opener"})
        check(opened.get("ok") and stub_calls == [str(path)] and "open now" in opened["message"], "daily_journal: opens the file through dispatch when open_file exists")
        quiet = await call("daily_journal", {"text": "quiet", "open": False})
        check(quiet.get("ok") and len(stub_calls) == 1, "daily_journal: open false does not open")
        TOOLS["open_file"]["fn"] = lambda path: {"ok": False, "error": "no app"}
        soft = await call("daily_journal", {"text": "still saved"})
        check(soft.get("ok") and "couldn't open" in soft["message"] and "still saved" in path.read_text(), "daily_journal: a failed open still saves the entry and says so")
    finally:
        TOOLS.pop("open_file", None)
        if real_open_file is not None:
            TOOLS["open_file"] = real_open_file
        common.now = real_now


async def check_screenshot_to_folder():
    reset_home("Pictures")
    real = TOOLS["take_screenshot"]["fn"]
    shots = []

    def fake_screenshot(save_path=None):
        shots.append(save_path)
        if save_path is None:
            return {"ok": False, "error": "stub refuses the default Desktop path"}
        Path(save_path).write_bytes(b"\x89PNG fake")
        return {"ok": True, "path": save_path}

    pinned = datetime(2026, 10, 12, 14, 30, 5)
    real_now = common.now
    common.now = lambda: pinned
    TOOLS["take_screenshot"]["fn"] = fake_screenshot
    try:
        first = await call("screenshot_to_folder", {})
        folder = HOME / "Pictures" / "Screenshots"
        expected = folder / "Screenshot 2026-10-12 at 14.30.05.png"
        check(first.get("ok") and first["path"] == str(expected) and expected.is_file(), f"screenshot_to_folder: saved into Pictures/Screenshots (stubbed capture) -- {first.get('error')}")
        second = await call("screenshot_to_folder", {})
        check(second.get("ok") and second["path"] == str(folder / "Screenshot 2026-10-12 at 14.30.05 (2).png") and expected.read_bytes() == b"\x89PNG fake", "screenshot_to_folder: same-second shot gets a free name, first not overwritten")
        named = await call("screenshot_to_folder", {"folder": str(HOME / "Pictures" / "Bugs"), "name": "../login: bug/"})
        saved = Path(named["path"])
        check(named.get("ok") and saved.parent == HOME / "Pictures" / "Bugs" and "/" not in saved.name and saved.name.endswith("14.30.05.png"), f"screenshot_to_folder: label cannot change the folder ({saved.name})")
        TOOLS["take_screenshot"]["fn"] = lambda save_path=None: {"ok": False, "error": "no screen recording permission"}
        failed = await call("screenshot_to_folder", {})
        check(failed.get("ok") is False and failed["failed_step"] == "take_screenshot" and "permission" in failed["error"], "screenshot_to_folder: capture failure is reported as that step")
    finally:
        TOOLS["take_screenshot"]["fn"] = real
        common.now = real_now
    check(all(s for s in shots), "screenshot_to_folder: never used the default Desktop path")


async def check_duplicate_report():
    downloads = reset_home("Downloads")
    documents = reset_home("Documents")
    blob = os.urandom(5000)
    put(downloads / "photo.jpg", blob)
    put(downloads / "photo copy.jpg", blob)
    put(downloads / "unique.txt", "u" * 3000)
    before = tree(downloads)
    result = await call("duplicate_report", {})
    check(result.get("ok") and result["duplicate_sets"] == 1 and result["report"], f"duplicate_report: finds the set in Downloads -- {result.get('error')}")
    report = Path(result["report"])
    text = report.read_text()
    check(report.parent == documents / "Leutheria Reports" and str(downloads / "photo copy.jpg") in text and "Nothing has been moved" in text, "duplicate_report: report written to Documents with the paths")
    check(tree(downloads) == before and "Nothing was removed" in result["message"], "duplicate_report: nothing moved or removed, and it says so")
    pinned = datetime(2026, 10, 12, 8, 0, 0)
    real_now = common.now
    common.now = lambda: pinned
    try:
        one = await call("duplicate_report", {})
        two = await call("duplicate_report", {})
    finally:
        common.now = real_now
    check(one["report"] != two["report"] and Path(one["report"]).read_text() == Path(two["report"]).read_text(), "duplicate_report: a second report in the same second does not overwrite the first")
    spoken = await call("duplicate_report", {"save_report": False})
    check(spoken.get("ok") and spoken["report"] is None and len(list((documents / "Leutheria Reports").iterdir())) == 3, "duplicate_report: save_report false writes nothing")
    clean = fresh("dupe_clean")
    put(clean / "only.txt", "one of a kind, long enough" * 100)
    none = await call("duplicate_report", {"path": str(clean)})
    check(none.get("ok") and none["duplicate_sets"] == 0 and none["report"] is None, "duplicate_report: no duplicates, no report")
    check((await call("duplicate_report", {"path": str(clean / "nope")})).get("ok") is False, "duplicate_report: missing folder")


async def check_extract_latest():
    downloads = reset_home("Downloads")
    make_zip(downloads / "old stuff.zip", {"old.txt": "old"})
    os.utime(downloads / "old stuff.zip", (time.time() - 5000, time.time() - 5000))
    make_zip(downloads / "new stuff.zip", {"inner/new.txt": "new"})
    put(downloads / "newest.pdf", "not an archive")
    put(downloads / "partial.zip.crdownload", "p")
    result = await call("extract_latest_download", {})
    check(result.get("ok") and result["archive"] == str(downloads / "new stuff.zip") and (downloads / "new stuff" / "inner" / "new.txt").read_text() == "new", f"extract_latest_download: unpacks the newest archive -- {result.get('error')}")
    check((downloads / "new stuff.zip").is_file() and not (downloads / "old stuff").exists(), "extract_latest_download: archive kept, older one ignored")
    again = await call("extract_latest_download", {})
    check(again.get("ok") and again["path"] == str(downloads / "new stuff (2)") and (downloads / "new stuff" / "inner" / "new.txt").read_text() == "new", "extract_latest_download: an existing folder is never written into; a new one is made")
    hostile = reset_home("Downloads")
    make_zip(hostile / "evil.zip", {"../../escaped.txt": "pwned"})
    bad = await call("extract_latest_download", {})
    check(bad.get("ok") is False and bad["failed_step"] == "extract_archive" and not (HOME / "escaped.txt").exists() and not (TMP / "escaped.txt").exists(), "extract_latest_download: a zip-slip archive is refused through the skill too")
    empty = reset_home("Downloads")
    put(empty / "readme.txt", "r")
    check((await call("extract_latest_download", {})).get("ok") is False, "extract_latest_download: no archive in Downloads")


async def main():
    sections = [
        check_registry, check_write_new_file, check_find_files, check_file_info, check_folder_size,
        check_zip_folder, check_extract_archive, check_find_duplicates, check_search_contents,
        check_spotlight, check_organize_folder, check_gating, check_organize_by_date,
        check_clean_desktop, check_collect_screenshots, check_archive_old_downloads,
        check_archive_folder, check_project_snapshot, check_reports, check_daily_journal,
        check_screenshot_to_folder, check_duplicate_report, check_extract_latest,
    ]
    for section in sections:
        try:
            await section()
        except Exception as e:  # a crash in one section is a failure, not the end of the report
            import traceback

            traceback.print_exc()
            check(False, f"{section.__name__} raised {type(e).__name__}: {e}")


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILED, {passed} passed")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print(f"all {passed} checks passed")
