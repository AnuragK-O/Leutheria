"""Where a tool may read and write. One policy, enforced in dispatch().

"Safe" used to mean only *how* a tool writes (never overwrites, only
appends, uses the Trash). It said nothing about *where*, and a create-only
write to ~/Library/LaunchAgents or an append to ~/.zshrc is code execution
with no prompt (security review 2026-10-13, C2). So every path argument is
declared in the tool's registry entry and judged here before the tool runs:

    "paths": {"source": "remove", "destination": "move_into"}

Modes (what the tool does with that argument):

    read       its content reaches the model (read_file, search, git_status)
    inspect    names, sizes, dates, or showing it on screen -- never content
    archive    packed or unpacked with every name kept (zip source, archive)
    copy_from  copied under a name the caller chooses (copy_file source)
    write      created, appended to, or extracted into
    remove     taken away from where it is (move source, trash)
    move_into  the destination of a move

A write-side argument lands in one of three zones:

    FREE  no prompt   user-content folders under the home folder
    ASK   confirm     anywhere else that is not denied
    DENY  refused     no prompt and no preference can override it

A read-side argument is either allowed or denied (credentials).

Everything is decided on the resolved path (symlinks followed) and by file
identity (device + inode) as well as by name, never by string prefix: the
default macOS volume is case-insensitive ("~/library" is ~/Library) and the
same folder is reachable under /System/Volumes/Data. A name comparison alone
misses both.

The roots are computed per call from the *effective* home folder, so a check
that points HOME at a temp directory gets the same zones there. Tests may add
to EXTRA_FREE_ROOTS; nothing else does.
"""

import os
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

READ, INSPECT, ARCHIVE, COPY_FROM = "read", "inspect", "archive", "copy_from"
WRITE, REMOVE, MOVE_INTO = "write", "remove", "move_into"
READ_MODES = frozenset({READ, INSPECT, ARCHIVE, COPY_FROM})
WRITE_MODES = frozenset({WRITE, REMOVE, MOVE_INTO})
MODES = READ_MODES | WRITE_MODES

FREE, ASK, DENY = "free", "ask", "deny"

# --- the zones ----------------------------------------------------------------

# Top-level folders of the home folder where safe tools write without asking.
# Each is here for a stated reason; a folder not listed asks.
FREE_HOME_FOLDERS = (
    # The user-content folders macOS creates for every account.
    "Documents",  # also holds ~/Documents/Journal (daily_journal) and
    #               ~/Documents/Leutheria Reports (duplicate_report)
    "Desktop",  # take_screenshot's default, clean_desktop, collect_screenshots
    "Downloads",  # organize_*, archive_old_downloads, extract_latest_download
    "Pictures",  # ~/Pictures/Screenshots (screenshot_to_folder, collect_screenshots)
    "Movies",
    "Music",
    # Folders the built-in skills write to by default.
    "Notes",  # quick_note, clipboard_to_note, clipboard_to_file, read_later, save_page_notes
    "Projects",  # setup_project, new_python_project, new_node_project, new_web_page, clone_repo
    "Scratch",  # code_scratchpad
    "Snapshots",  # project_snapshot
)

# Never writable. Outside the home folder (and outside EXTRA_FREE_ROOTS).
SYSTEM_ROOTS = (
    "/System", "/Library", "/usr", "/bin", "/sbin", "/etc", "/var", "/opt", "/dev", "/cores",
    "/Applications", "/private/etc", "/private/var",
)

# A move into one of these is a delete with a delay.
TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp")
TRASH_NAMES = frozenset({".trash", ".trashes"})

# Folders Finder shows as one file. Writing inside one edits an app, a photo
# library or a project file behind its owner's back; creating one makes a
# launchable thing out of plain files. fs_common.BUNDLE_SUFFIXES plus the
# code-carrying kinds it has no reason to know about.
BUNDLE_SUFFIXES = (
    ".app", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".framework", ".bundle",
    ".xcodeproj", ".xcworkspace", ".logicx", ".imovielibrary",
    ".plugin", ".kext", ".appex", ".xpc", ".prefpane", ".saver", ".qlgenerator",
    ".mdimporter", ".workflow", ".action", ".service", ".component", ".wdgt",
)

# Top-level entries of the home folder that are never read: credentials, and
# shell histories (people paste tokens into shells).
SECRET_HOME_ENTRIES = frozenset(
    name.casefold()
    for name in (
        ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".netrc", ".npmrc", ".pypirc",
        ".git-credentials", ".password-store", ".1password", ".op", ".terraform.d", ".vault-token",
        ".pgpass", ".my.cnf", ".boto", ".s3cfg", ".gem", ".mozilla", ".electrum", ".bitcoin",
        ".zsh_history", ".bash_history", ".python_history", ".node_repl_history", ".psql_history",
        ".mysql_history", ".lesshst", ".viminfo", ".zsh_sessions",
    )
)
# Children of ~/.config (and ~/.local/share) that hold tokens or browser profiles.
SECRET_CONFIG_ENTRIES = frozenset(
    name.casefold()
    for name in (
        "gh", "gcloud", "op", "hub", "rclone", "doctl", "stripe", "sops", "heroku", "netlify",
        "configstore", "1password", "google-chrome", "chromium", "bravesoftware", "keyrings",
        "gnupg", "aws", "azure", "fly", "vercel", "anthropic", "openai",
    )
)
# Folder names that mean "credentials" wherever they are (a backup of ~/.ssh).
SECRET_DIR_NAMES = frozenset({".ssh", ".gnupg", ".aws", "keychains"})

SECRET_FILE_NAMES = frozenset(
    {".netrc", ".npmrc", ".pypirc", ".git-credentials", ".htpasswd", ".pgpass", ".vault-token"}
)
SECRET_FILE_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
SECRET_FILE_SUFFIXES = (
    ".pem", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".ovpn", ".keychain", ".keychain-db",
)

# Set by the check scripts only: extra folders treated like a user-content
# folder (their temp work directory, which sits under /private/var).
EXTRA_FREE_ROOTS: list = []

REPO_ROOT = Path(__file__).resolve().parents[3]  # the folder holding agent/ and app/


def _main_checkout(root: Path):
    """A git worktree's .git is a file pointing into the main checkout. The
    main checkout is the same program, so it is protected too."""
    try:
        text = (root / ".git").read_text().strip() if (root / ".git").is_file() else ""
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:"):].strip())
    for parent in gitdir.parents:
        if parent.name == ".git":
            return parent.parent
    return None


_MAIN_CHECKOUT = _main_checkout(REPO_ROOT)

# (module, attribute, readable). Looked up at call time through sys.modules so
# a check that redirects one of these protects the redirected file, and so
# this module imports nothing from the agent.
_STATE_ATTRS = (
    ("agent.core.preferences", "PREFS_FILE"),
    ("agent.core.trust", "TRUST_FILE"),
    ("agent.core.voice_settings", "SETTINGS_FILE"),
    ("agent.core.weather", "HOME_FILE"),
    ("agent.core.logging_util", "LOG_DIR"),
    ("agent.skills.registry", "GENERATED_DIR"),
)
_STATE_NAMES = ("preferences.json", "trusted_commands.json", "voice_settings.json", "home_location.json", "registry_state.json", "logs")


# --- path identity --------------------------------------------------------------


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _ident(path: str):
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


class _Root:
    """A folder (or file) to compare against, resolved once."""

    __slots__ = ("path", "fold", "ident")

    def __init__(self, path):
        self.path = os.path.realpath(os.path.abspath(str(path)))
        self.fold = _fold(self.path)
        self.ident = _ident(self.path)


class _Subject:
    """A resolved path and its chain of ancestors, each with its identity."""

    def __init__(self, path: str):
        self.path = path
        self.parts = Path(path).parts
        self.chain = []  # (number of leading parts, folded path, ident), deepest first
        current = Path(path)
        while True:
            text = str(current)
            self.chain.append((len(current.parts), _fold(text), _ident(text)))
            if current.parent == current:
                break
            current = current.parent

    def under(self, root: _Root):
        """The path's components below `root` (() if it *is* root), or None
        if it is not inside it. Same folder means same name ignoring case,
        or the same device and inode."""
        for count, fold, ident in self.chain:
            if fold == root.fold or (ident is not None and ident == root.ident):
                return self.parts[count:]
        return None

    @property
    def name(self) -> str:
        return self.parts[-1] if len(self.parts) > 1 else ""


def _lexical(raw: str) -> str:
    """The absolute path a tool will act on, exactly as the tools compute it
    (expanduser, then relative to the working directory)."""
    return os.path.abspath(os.path.expanduser(raw))


def homes() -> list:
    """The effective home folder(s). Normally one. The file skills can be
    pointed at another (files_common.HOME_DIR / LEUTHERIA_FILES_HOME), and
    that one is then a home as well."""
    found = [str(Path.home())]
    override = getattr(sys.modules.get("agent.skills.files_common"), "HOME_DIR", None) or os.environ.get(
        "LEUTHERIA_FILES_HOME"
    )
    if override:
        found.append(os.path.expanduser(str(override)))
    return found


class _Roots:
    def __init__(self):
        self.homes = [_Root(h) for h in dict.fromkeys(homes())]
        trees = [REPO_ROOT] + ([_MAIN_CHECKOUT] if _MAIN_CHECKOUT else [])
        self.agent_trees = [_Root(t) for t in trees]
        state = []
        for module, attr in _STATE_ATTRS:
            value = getattr(sys.modules.get(module), attr, None)
            if value:
                state.append(value)
        prompt = getattr(sys.modules.get("agent.core.agent_loop"), "SYSTEM_PROMPT_PATH", None)
        self.state_write = [_Root(p) for p in state + ([prompt] if prompt else [])]
        # Readable: the system prompt (the model has it already) and the
        # source tree. Not readable: settings, approvals, the log, the key.
        # Each tree's own copies too: a worktree's agent must not read the
        # main checkout's approvals or log, and the reverse.
        fixed = [t / ".env" for t in trees] + [t / "agent" / name for t in trees for name in _STATE_NAMES]
        self.state_read = [_Root(p) for p in state + fixed]
        self.extra_free = [_Root(p) for p in EXTRA_FREE_ROOTS]
        self.system = [_Root(p) for p in SYSTEM_ROOTS]
        self.temp = [_Root(p) for p in dict.fromkeys((*TEMP_ROOTS, tempfile.gettempdir()))]
        self.volumes = _Root("/Volumes")
        self.made = time.monotonic()


_roots_cache = None
_ROOTS_TTL = 2.0  # seconds; the roots cost ~40 stat calls and a bulk skill makes hundreds of checks


def _roots() -> _Roots:
    global _roots_cache
    key = (tuple(homes()), tuple(map(str, EXTRA_FREE_ROOTS)), tuple(
        str(getattr(sys.modules.get(m), a, "")) for m, a in _STATE_ATTRS
    ))
    cached = _roots_cache
    if cached is None or cached[0] != key or time.monotonic() - cached[1].made > _ROOTS_TTL:
        cached = (key, _Roots())
        _roots_cache = cached
    return cached[1]


def reset_cache() -> None:
    global _roots_cache
    _roots_cache = None


# --- names ----------------------------------------------------------------------


def _is_bundle_name(name: str) -> bool:
    return name.casefold().endswith(BUNDLE_SUFFIXES)


def _is_keynote(path: str) -> bool:
    """A .key file is a Keynote presentation far more often than a private
    key, and Keynote files are zip archives (or, older, folders)."""
    try:
        if os.path.isdir(path):
            return True
        with open(path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def secret_name(name: str, path: str = None) -> bool:
    """Whether a file name alone marks it as credentials."""
    folded = name.casefold()
    if folded == ".env" or folded.startswith(".env."):
        return True
    if folded in SECRET_FILE_NAMES or folded.startswith(SECRET_FILE_PREFIXES) or folded.endswith(SECRET_FILE_SUFFIXES):
        return True
    if folded.endswith(".key"):
        return path is None or not _is_keynote(path)
    return False


# --- verdicts -------------------------------------------------------------------


class Verdict:
    __slots__ = ("zone", "why", "path")

    def __init__(self, zone: str, why: str, path: str):
        self.zone, self.why, self.path = zone, why, path

    def __repr__(self):
        return f"Verdict({self.zone!r}, {self.why!r}, {self.path!r})"


def _read_denial(subject: _Subject, roots: _Roots, check_name: bool):
    """Why this path may not be read, or None."""
    for root in roots.state_read:
        if subject.under(root) is not None:
            return "it is one of Leutheria's own settings, approvals, logs or keys"
    for home in roots.homes:
        rel = subject.under(home)
        if not rel:
            continue
        top = rel[0].casefold()
        if top == "library":
            return "it is inside the Library folder, where apps keep passwords, cookies and browser profiles"
        if top in SECRET_HOME_ENTRIES:
            return "it holds credentials or shell history"
        if top in (".config", ".local") and any(part.casefold() in SECRET_CONFIG_ENTRIES for part in rel[1:3]):
            return "it holds an app's saved sign-in"
    folded_parts = [part.casefold() for part in subject.parts]
    if any(part in SECRET_DIR_NAMES for part in folded_parts[:-1]) or (
        folded_parts and folded_parts[-1] in SECRET_DIR_NAMES
    ):
        return "it is a credentials folder"
    if check_name and subject.name and secret_name(subject.name, subject.path):
        return "its name marks it as a key, password or secrets file"
    return None


def _write_zone(subject: _Subject, roots: _Roots, mode: str) -> tuple:
    """(zone, why) for a path that would be created, changed or taken away."""
    for root in roots.agent_trees + roots.state_write:
        if subject.under(root) is not None:
            return DENY, "it is part of Leutheria itself (its program, settings, approvals or logs)"

    folded_parts = [part.casefold() for part in subject.parts]
    inner, last = folded_parts[:-1], (folded_parts[-1] if len(folded_parts) > 1 else "")
    if any(_is_bundle_name(part) for part in inner):
        return DENY, "it is inside an application, library or other package that manages its own files"
    if ".git" in folded_parts[1:]:
        return DENY, "it is inside a repository's .git folder, which git manages and which can run commands"
    bundle_whole = _is_bundle_name(last)
    if bundle_whole and mode == WRITE:
        return DENY, "it would create an application or other package"
    if mode == MOVE_INTO and any(part in TRASH_NAMES for part in folded_parts):
        return DENY, "moving something into the Trash folder directly is a delete; use trash_file"

    verdicts = []
    for home in roots.homes:
        rel = subject.under(home)
        if rel is None:
            continue
        if not rel:
            verdicts.append((DENY, "it is the home folder itself") if mode != WRITE else (ASK, "it is the home folder itself"))
            continue
        top = rel[0].casefold()
        if top.startswith("."):
            verdicts.append((DENY, "hidden files and folders at the top of the home folder configure the shell, SSH, git and other programs"))
        elif top == "library":
            verdicts.append((DENY, "it is inside the Library folder, which apps and macOS manage and which can start programs at login"))
        elif top == "applications":
            verdicts.append((DENY, "it is inside the Applications folder"))
        elif top in _FREE_FOLDED:
            if mode == REMOVE and len(rel) == 1:
                verdicts.append((ASK, f"it is your whole {rel[0]} folder"))
            elif bundle_whole:
                # Moving an existing app is ordinary; renaming a folder to
                # Something.app is how plain files become launchable.
                verdicts.append((ASK, "it is a whole application or package"))
            else:
                verdicts.append((FREE, ""))
        else:
            verdicts.append((ASK, "it is outside your usual folders"))
    if verdicts:
        for zone in (DENY, ASK, FREE):
            for verdict in verdicts:
                if verdict[0] == zone:
                    return verdict

    for root in roots.extra_free:
        rel = subject.under(root)
        if rel is not None:
            return (ASK, "it is a whole work folder") if (mode == REMOVE and not rel) else (FREE, "")

    for root in roots.temp:
        if subject.under(root) is not None:
            if mode == MOVE_INTO:
                return DENY, "a temporary folder is emptied by the system, so moving something there deletes it"
            return ASK, "it is in a temporary folder outside your usual folders"
    for root in roots.system:
        if subject.under(root) is not None:
            return DENY, "it is a system folder"
    if subject.path == os.sep:
        return DENY, "it is the top of the disk"
    return ASK, "it is outside your home folder" if subject.under(roots.volumes) is None else "it is on another disk"


_FREE_FOLDED = frozenset(name.casefold() for name in FREE_HOME_FOLDERS)


def _subjects(raw: str, mode: str) -> tuple:
    """(subjects, symlink_error). Usually one subject; two when the text has
    surrounding whitespace, because some tools strip it and some do not, and
    whichever the tool does must have been checked."""
    variants = list(dict.fromkeys((raw, raw.strip())))
    subjects = []
    for text in variants:
        if not text:
            continue
        lexical = _lexical(text)
        if mode in (WRITE, MOVE_INTO) and os.path.islink(lexical):
            return [], lexical
        if mode == REMOVE:
            # What goes away is the entry itself, even if it is a link: its
            # folder is resolved, its own name is not.
            parent, name = os.path.split(lexical)
            resolved = os.path.join(os.path.realpath(parent), name) if name else os.path.realpath(lexical)
        else:
            resolved = os.path.realpath(lexical)
        subjects.append(_Subject(resolved))
    return subjects, None


def classify(raw, mode: str) -> Verdict:
    """Judge one path for one mode. Read-side modes return FREE or DENY."""
    if mode not in MODES:
        raise ValueError(f"unknown path mode: {mode}")
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        # Not a usable path; the tool reports that in its own words.
        return Verdict(FREE, "", "")
    subjects, link = _subjects(raw, mode)
    if link:
        return Verdict(DENY, "it is a symbolic link, and writing through a link changes a file somewhere else", link)
    roots = _roots()
    worst = Verdict(FREE, "", subjects[0].path if subjects else "")
    for subject in subjects:
        if mode in READ_MODES:
            why = _read_denial(subject, roots, check_name=(mode == READ))
            verdict = Verdict(DENY, why, subject.path) if why else Verdict(FREE, "", subject.path)
        else:
            zone, why = _write_zone(subject, roots, mode)
            if zone != DENY and mode == REMOVE:
                # Taking a credentials folder away is also how it would be
                # moved somewhere readable.
                denial = _read_denial(subject, roots, check_name=False)
                if denial:
                    zone, why = DENY, denial
            verdict = Verdict(zone, why, subject.path)
        if (verdict.zone, worst.zone) in ((DENY, FREE), (DENY, ASK), (ASK, FREE)):
            worst = verdict
    return worst


# --- a whole call ---------------------------------------------------------------

_VERB = {
    READ: "read", INSPECT: "look at", ARCHIVE: "read", COPY_FROM: "copy",
    WRITE: "write to", REMOVE: "move or remove", MOVE_INTO: "move something into",
}

# Property names that mean "this argument is a path".
_PATH_NAMES = frozenset(
    {"path", "paths", "file", "folder", "dir", "directory", "source", "destination", "archive", "target", "location"}
)
_PATH_SUFFIXES = ("_path", "_paths", "_file", "_folder", "_dir", "_directory")


def path_like_args(tool: dict) -> list:
    """Arguments of a tool's schema that look like filesystem paths."""
    found = []
    properties = (tool.get("input_schema") or {}).get("properties") or {}
    for name, spec in properties.items():
        lowered = name.lower()
        described = str((spec or {}).get("description", "")).lower()
        if lowered in _PATH_NAMES or lowered.endswith(_PATH_SUFFIXES) or " path " in f" {described} ".replace(".", " ").replace(",", " "):
            found.append(name)
    return found


def undeclared_path_args(tool: dict) -> list:
    """Path-like arguments with no declared mode. Must be empty for every
    tool: dispatch() refuses a tool for which it is not, so a new tool
    cannot reach the filesystem without saying where it reads and writes.
    An argument that only looks like a path is listed under "not_paths"."""
    declared = set(tool.get("paths") or {}) | set(tool.get("not_paths") or ())
    return [name for name in path_like_args(tool) if name not in declared]


def _spec(value) -> tuple:
    """A "paths" value is a mode, or {"mode": ..., "default_beside": <arg>}:
    when the argument is left out the tool writes next to that other one."""
    if isinstance(value, dict):
        return value.get("mode"), value.get("default_beside")
    return value, None


class CallVerdict:
    """The outcome for one tool call: an error (refused), or the write
    targets that need the user's say-so first."""

    __slots__ = ("error", "denied", "asks")

    def __init__(self):
        self.error = None
        self.denied = None  # (arg, path, why)
        self.asks = []  # [(arg, path, why)]


def check_call(tool_name: str, tool: dict, args: dict) -> CallVerdict:
    result = CallVerdict()
    missing = undeclared_path_args(tool)
    if missing:
        result.error = (
            f"{tool_name} is not available: its argument {missing[0]!r} is a path with no declared "
            "read/write policy (a bug in the tool's registry entry, not something to retry)."
        )
        result.denied = (missing[0], "", "undeclared path argument")
        return result

    declared = tool.get("paths") or {}
    judged = []  # (arg, mode, raw, verdict)
    for arg, value in declared.items():
        mode, beside = _spec(value)
        raw = args.get(arg)
        if (raw is None or raw == "") and beside and isinstance(args.get(beside), str) and args[beside].strip():
            # The tool will pick a name next to the other argument.
            raw = os.path.join(os.path.dirname(_lexical(args[beside].strip())), "new item")
        if isinstance(raw, (list, tuple)):
            values = [item for item in raw if isinstance(item, str)]
        else:
            values = [raw]
        for item in values:
            verdict = classify(item, mode)
            judged.append((arg, mode, item, verdict))
            if verdict.zone == DENY:
                return _refuse(result, tool_name, arg, mode, verdict)

    # A credentials file may be copied or moved only if it keeps a name that
    # still marks it as one. Otherwise "copy id_rsa to notes.txt, then read
    # notes.txt" walks around the read rule.
    renamers = [j for j in judged if j[1] in (COPY_FROM, REMOVE) and isinstance(j[2], str) and j[2].strip()]
    targets = [j for j in judged if j[1] in (WRITE, MOVE_INTO) and isinstance(j[2], str) and j[2].strip()]
    for arg, mode, raw, verdict in renamers:
        name = os.path.basename(verdict.path)
        if not targets or not secret_name(name, os.path.realpath(_lexical(raw.strip()))):
            continue
        if any(not secret_name(os.path.basename(t[3].path)) for t in targets):
            return _refuse(
                result, tool_name, arg, mode,
                Verdict(DENY, "its name marks it as a key, password or secrets file, and it may only be moved or copied under the same kind of name", verdict.path),
            )

    result.asks = [(arg, verdict.path, verdict.why) for arg, _, _, verdict in judged if verdict.zone == ASK]
    return result


def _refuse(result: CallVerdict, tool_name: str, arg: str, mode: str, verdict: Verdict) -> CallVerdict:
    result.denied = (arg, verdict.path, verdict.why)
    result.error = (
        f"Not allowed: Leutheria may never {_VERB[mode]} {verdict.path} because {verdict.why}. "
        "This is a fixed safety rule; no confirmation or setting overrides it. Do not retry with "
        "another spelling of the path or another tool. Tell the user it cannot be done here."
    )
    return result


# --- helpers for tools and skills --------------------------------------------------


class ReadFilter:
    """Per-file read check for a tool that walks a tree and returns content
    (search_file_contents). Folder decisions are cached, so a walk costs one
    name test per file and one full check per folder."""

    def __init__(self):
        self._folders = {}

    def allows(self, file_path: str) -> bool:
        folder, name = os.path.split(file_path)
        if secret_name(name, file_path):
            return False
        allowed = self._folders.get(folder)
        if allowed is None:
            allowed = classify(folder, INSPECT).zone != DENY
            self._folders[folder] = allowed
        return allowed


def bulk_refusal(folder) -> str:
    """Why a bulk skill (organise, tidy, sort by date) must not rearrange
    this folder, or None. Stricter than the write zones: moving two hundred
    files by type is wrong in places where moving one is fine."""
    text = str(folder)
    if not text.strip():
        return "no folder was given"
    lexical = _lexical(text)
    probe = classify(os.path.join(lexical, "a file"), REMOVE)
    if probe.zone == DENY:
        return probe.why.replace("it is", "that is", 1)
    subject = _Subject(os.path.realpath(lexical))
    roots = _roots()
    if subject.path == os.sep:
        return "that is the top of the disk, not a folder of loose files"
    if _is_bundle_name(subject.name):
        return "that is an application, library or other package that manages its own files"
    volume = subject.under(roots.volumes)
    if volume is not None and len(volume) <= 1:
        return "that is a whole disk, not a folder of loose files"
    inside = False
    for home in roots.homes:
        rel = subject.under(home)
        if rel is not None:
            if not rel:
                return "that is the top of the home folder, not a folder of loose files"
            inside = True
    if not inside and volume is None and not any(subject.under(root) is not None for root in roots.extra_free):
        return "that is outside your home folder"
    current = Path(subject.path)
    while True:
        if os.path.lexists(current / ".git"):
            return "it is part of a code project, and moving its files would break it"
        if current.parent == current or any(_fold(str(current)) == home.fold for home in roots.homes):
            break
        current = current.parent
    return None
