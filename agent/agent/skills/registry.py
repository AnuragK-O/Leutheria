import functools
import json
import os
import time
from pathlib import Path

from agent.core.logging_util import log_event
from agent.skills import backup_folder, quick_note, setup_project, validate
from agent.skills.template_skill import run_template
from agent.skills.validate import SkillValidationError, skill_path
from agent.tools.registry import TOOLS

# --- dev/web ---
from agent.skills import (
    clipboard_to_file,
    clipboard_to_note,
    clone_repo,
    code_scratchpad,
    define_term,
    git_snapshot,
    git_sync,
    github_open,
    new_node_project,
    new_python_project,
    new_web_page,
    open_workspace,
    read_later,
    repo_overview,
    research_topic,
    save_page_notes,
    web_search,
)

GENERATED_DIR = Path(__file__).resolve().parent / "generated"

# A skill is a multi-step procedure exposed to the LLM as if it were a single
# tool. Its "fn" gets (args, websocket, pending) instead of **args, since it
# needs to call back into dispatch() for its own sub-steps (so a skill's
# destructive actions still go through the normal per-step confirmation flow
# -- a skill isn't a way to bypass safety tiering, just a way to avoid
# re-deriving the same multi-step plan via LLM reasoning every time).
#
# Hand-written skills (like setup_project) live in their own package with a
# real run() function. Generated skills (agent-proposed, user-approved) are
# plain JSON step lists under generated/, executed by the one shared,
# reviewed template_skill.run_template() interpreter -- a generated skill is
# data, never new code.
SKILLS = {
    "setup_project": {
        "fn": setup_project.run,
        "description": (
            "Create a new software project: make a folder, run `git init` inside it, "
            "and open it in VS Code. Use this whenever the user wants to start or set "
            "up a new project/repo -- it's faster and more reliable than composing the "
            "individual steps yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The project/folder name, e.g. 'sample'."},
                "base_dir": {
                    "type": "string",
                    "description": "Parent directory to create the project in. Defaults to '~/Projects' if omitted.",
                },
            },
            "required": ["name"],
        },
    },
    "quick_note": {
        "fn": quick_note.run,
        "description": (
            "Append a timestamped line to a plain text log file and confirm with a "
            "notification. This is a scratch log, NOT the Notes app and not a document "
            "the user can see, open or edit -- nothing appears on screen. Use it only "
            "for 'remind me later' or 'jot this down somewhere' where the user doesn't "
            "care where it lands. If they name an app, or want a note or document they "
            "can look at afterwards, open that app and write it there instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The note content."},
                "notes_path": {
                    "type": "string",
                    "description": "Where to save notes. Defaults to '~/Notes/notes.txt' if omitted.",
                },
            },
            "required": ["text"],
        },
    },
    "backup_folder": {
        "fn": backup_folder.run,
        "description": (
            "Create a timestamped backup copy of a file or folder, alongside the original. "
            "Use this whenever the user wants to back up or duplicate something before changing it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of the file or folder to back up."},
            },
            "required": ["path"],
        },
    },
    # --- dev/web ---
    "new_python_project": {
        "fn": new_python_project.run,
        "description": (
            "Scaffold a new Python project with starter files: a folder holding a runnable "
            "main.py, a README, requirements.txt and a gitignore. Use this when the user "
            "asks for a Python project, script or app by name. It only writes files; git "
            "init and a virtual environment happen only if asked for, and each of those "
            "asks the user first. It refuses if the folder already has anything in it. "
            "Not for an empty project folder with no language named (that is "
            "setup_project), and not for Node or web pages (new_node_project, new_web_page)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The project and folder name, e.g. 'atlas'."},
                "base_dir": {"type": "string", "description": "Parent folder. Defaults to '~/Projects'."},
                "git_init": {"type": "boolean", "description": "Also run git init in it. Only if the user asked."},
                "create_venv": {
                    "type": "boolean",
                    "description": "Also create a .venv virtual environment in it. Only if the user asked.",
                },
                "open_in": {
                    "type": "string",
                    "description": "Application to open the folder in afterwards, e.g. 'Visual Studio Code'. Omit to open nothing.",
                },
            },
            "required": ["name"],
        },
    },
    "new_node_project": {
        "fn": new_node_project.run,
        "description": (
            "Scaffold a new Node.js project with starter files: a folder holding "
            "package.json, a runnable index.js, a README and a gitignore. Use this when "
            "the user asks for a Node or JavaScript project by name. It only writes "
            "files; git init and npm install happen only if asked for, and each asks the "
            "user first. It refuses if the folder already has anything in it. Not for a "
            "page to open in a browser (new_web_page), Python (new_python_project), or an "
            "empty project folder with no language named (setup_project)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The project and folder name, e.g. 'atlas-api'."},
                "base_dir": {"type": "string", "description": "Parent folder. Defaults to '~/Projects'."},
                "git_init": {"type": "boolean", "description": "Also run git init in it. Only if the user asked."},
                "install": {"type": "boolean", "description": "Also run npm install in it. Only if the user asked."},
                "open_in": {
                    "type": "string",
                    "description": "Application to open the folder in afterwards, e.g. 'Visual Studio Code'. Omit to open nothing.",
                },
            },
            "required": ["name"],
        },
    },
    "new_web_page": {
        "fn": new_web_page.run,
        "description": (
            "Scaffold a new static web page: a folder holding index.html, styles.css and "
            "script.js, wired together and ready to open in a browser. Use this when the "
            "user wants a new web page, landing page or HTML and CSS starter. It refuses "
            "if the folder already has anything in it. Not for opening or searching an "
            "existing website (open_url, web_search), and not for a Node project "
            "(new_node_project)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The folder name, e.g. 'portfolio'."},
                "title": {"type": "string", "description": "The page's title and heading. Defaults to the name."},
                "base_dir": {"type": "string", "description": "Parent folder. Defaults to '~/Projects'."},
                "open_in_browser": {"type": "boolean", "description": "Open the new page in the browser afterwards."},
                "open_in": {
                    "type": "string",
                    "description": "Application to open the folder in afterwards, e.g. 'Visual Studio Code'. Omit to open nothing.",
                },
            },
            "required": ["name"],
        },
    },
    "git_snapshot": {
        "fn": git_snapshot.run,
        "description": (
            "Commit everything that has changed in a git repository: stage all changes "
            "and commit them with the given message. It checks the repository's state "
            "itself, so call it directly without running git_status first. The user is "
            "shown the command and asked before it runs. It stops without committing if there is nothing to "
            "commit, if there are merge conflicts, or if a changed file looks like it "
            "holds secrets. It does not push (git_sync does) and it cannot commit only "
            "some files. Not for a copy of a folder outside git: that is backup_folder."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "A folder inside the repository, e.g. '~/Projects/atlas'."},
                "message": {"type": "string", "description": "The commit message."},
                "allow_sensitive": {
                    "type": "boolean",
                    "description": "Commit even though a file looks like it holds secrets. Only after the user, told which file, said to include it.",
                },
            },
            "required": ["path", "message"],
        },
    },
    "git_sync": {
        "fn": git_sync.run,
        "description": (
            "Pull the latest changes into a git repository, or push its commits to the "
            "remote. It checks the repository itself, so call it directly without running "
            "git_status first, and never skip a pull because git_status showed nothing "
            "behind, since git_status does not contact the remote. The user is shown the "
            "command and asked before it runs. A pull only "
            "fast-forwards: if it would need a merge it stops and says so. It does not "
            "commit anything first; use git_snapshot for that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "A folder inside the repository."},
                "action": {"type": "string", "enum": ["pull", "push"], "description": "Which direction to sync."},
            },
            "required": ["path", "action"],
        },
    },
    "clone_repo": {
        "fn": clone_repo.run,
        "description": (
            "Clone a git repository from GitHub, or any https address, into a new folder. "
            "The user is shown the command and asked before it runs. It refuses if the "
            "destination folder already exists. Not for looking at a repository in the "
            "browser: that is github_open."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "The repository as 'owner/name' on GitHub, or a full https clone address.",
                },
                "base_dir": {"type": "string", "description": "Parent folder to clone into. Defaults to '~/Projects'."},
                "name": {"type": "string", "description": "Folder name for the clone. Defaults to the repository's name."},
                "open_in": {
                    "type": "string",
                    "description": "Application to open the clone in afterwards, e.g. 'Visual Studio Code'. Omit to open nothing.",
                },
            },
            "required": ["repo"],
        },
    },
    "repo_overview": {
        "fn": repo_overview.run,
        "description": (
            "Describe a git repository on this Mac in one go: branch and uncommitted "
            "changes, recent commits, what kind of project it is, what is at its top "
            "level, and the start of its README. Read-only, nothing appears on screen. "
            "Use it for questions like what is this repo or what is the state of this "
            "project. For only the branch and changed files, git_status alone is enough. "
            "Not for a folder that is not a git repository: use list_files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "A folder inside the repository, e.g. '~/Projects/atlas'."},
            },
            "required": ["path"],
        },
    },
    "open_workspace": {
        "fn": open_workspace.run,
        "description": (
            "Open an existing project folder in a code editor (Visual Studio Code unless "
            "another is named) and report its git branch and changes if it is a "
            "repository. Use it when the user wants to start working on a project that "
            "already exists. Not for creating a project (setup_project, "
            "new_python_project, new_node_project, new_web_page) and not for opening a "
            "single file or a non-editor app, which is open_with."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The project folder, e.g. '~/Projects/atlas'."},
                "app": {"type": "string", "description": "Editor to open it in. Defaults to 'Visual Studio Code'."},
            },
            "required": ["path"],
        },
    },
    "web_search": {
        "fn": web_search.run,
        "description": (
            "Open a search results page in the user's browser for them to look at, on "
            "Google unless another site is named. With no query it reads the clipboard "
            "itself and searches for that text. Use it when the user says search for, look up or google "
            "something. It shows results, it does not read them: when the user wants you "
            "to tell them the answer, answer directly or read a page with fetch_url. For "
            "several starting points on one topic use research_topic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for. Omit to search for the clipboard's text."},
                "engine": {
                    "type": "string",
                    "enum": [
                        "google", "duckduckgo", "bing", "youtube", "wikipedia", "maps", "images",
                        "news", "scholar", "reddit", "stackoverflow", "github",
                    ],
                    "description": "Which site to search. Defaults to google.",
                },
            },
            "required": [],
        },
    },
    "research_topic": {
        "fn": research_topic.run,
        "description": (
            "Open a small set of browser tabs as starting points for researching a topic: "
            "by default a web search, Wikipedia and news, four tabs at most. Use it when "
            "the user says research this or wants to read up on something. It opens "
            "pages for the user to read and does not read or summarize anything itself. "
            "For one ordinary search use web_search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "The topic, in a few words."},
                "sources": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["web", "wikipedia", "news", "scholar", "videos", "discussion"],
                    },
                    "description": "Which kinds of source to open, four at most. Defaults to web, wikipedia and news.",
                },
            },
            "required": ["topic"],
        },
    },
    "define_term": {
        "fn": define_term.run,
        "description": (
            "Open the dictionary page for a word or phrase in the user's browser, or its "
            "thesaurus, Wiktionary or Wikipedia page. Use it when the user asks to look a "
            "word up or to see its definition. It opens a page and does not return the "
            "definition: if the user just asks what a word means, tell them yourself "
            "instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "term": {"type": "string", "description": "The word or phrase."},
                "source": {
                    "type": "string",
                    "enum": ["dictionary", "thesaurus", "wiktionary", "wikipedia"],
                    "description": "Where to look it up. Defaults to dictionary.",
                },
            },
            "required": ["term"],
        },
    },
    "github_open": {
        "fn": github_open.run,
        "description": (
            "Open a GitHub page in the user's browser: a repository by owner and name, "
            "the repository a local folder was cloned from, one section of it such as "
            "issues or pull requests, a user's profile, or a GitHub search. With no "
            "arguments it opens the GitHub home page. It only opens a page and does not "
            "download or read anything. To download "
            "a repository use clone_repo, and for the state of a local one use git_status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "The repository as 'owner/name', or just an owner for their profile."},
                "path": {
                    "type": "string",
                    "description": "A local repository folder, to open the GitHub page it was cloned from. Used when repo is omitted.",
                },
                "section": {
                    "type": "string",
                    "enum": ["code", "issues", "pulls", "actions", "releases", "commits", "branches", "wiki"],
                    "description": "Which part of the repository to open. Defaults to code.",
                },
                "query": {"type": "string", "description": "Search text: within the repository if one is given, otherwise across GitHub."},
            },
            "required": [],
        },
    },
    "clipboard_to_note": {
        "fn": clipboard_to_note.run,
        "description": (
            "Append whatever text is on the clipboard, with a timestamp, to a running "
            "plain text file. Use it when the user says save what I copied or save my "
            "clipboard to a note. It reads the clipboard itself, so call it directly "
            "without get_clipboard first. Like quick_note this is a scratch file and nothing "
            "appears on screen, not the Notes app. When the user dictates the text "
            "themselves use quick_note; when they want the clipboard as its own separate "
            "file use clipboard_to_file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "A few words saying what this is, put before the text. Optional."},
                "notes_path": {"type": "string", "description": "File to append to. Defaults to '~/Notes/clipboard.txt'."},
            },
            "required": [],
        },
    },
    "clipboard_to_file": {
        "fn": clipboard_to_file.run,
        "description": (
            "Save whatever text is on the clipboard as a new file of its own. Use it when "
            "the user wants the copied text as a file, especially one they name. It reads "
            "the clipboard itself, so call it directly without get_clipboard first. It "
            "never overwrites: if the file exists it stops and says so. To add the "
            "clipboard to a running notes file use clipboard_to_note, and for a new code "
            "file that opens in an editor use code_scratchpad."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path of the new file, e.g. '~/Documents/quote.txt'. Defaults to a timestamped file in '~/Notes'.",
                },
                "open_in": {"type": "string", "description": "Application to open the new file in, e.g. 'TextEdit'. Omit to open nothing."},
            },
            "required": [],
        },
    },
    "save_page_notes": {
        "fn": save_page_notes.run,
        "description": (
            "Fetch a web page and save its text as a new Markdown file, with its title, "
            "address and the date, so the content is kept. With no address it reads the "
            "one on the clipboard itself. Use it when the user wants a page's content saved or "
            "archived. It saves the page's own text, not a summary, and nothing appears "
            "on screen. To remember just the link use read_later, and to read a page "
            "without saving it use fetch_url."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The page's address. Omit to use the address on the clipboard."},
                "note": {"type": "string", "description": "A line of the user's own to put at the top. Optional."},
                "folder": {"type": "string", "description": "Folder to save into. Defaults to '~/Notes/pages'."},
            },
            "required": [],
        },
    },
    "read_later": {
        "fn": read_later.run,
        "description": (
            "Add a web address to the user's reading list, a plain text file with one "
            "dated line per link. With no address it reads the one on the clipboard "
            "itself, so there is no need to call get_clipboard first. Use "
            "it when the user says save this for later or add this to my reading list. "
            "It keeps only the link and fetches nothing; it is not a browser bookmark "
            "and nothing appears on screen. To keep the page's content use "
            "save_page_notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The address to save. Omit to use the address on the clipboard."},
                "title": {"type": "string", "description": "What to call it in the list. Optional."},
                "list_path": {"type": "string", "description": "The reading list file. Defaults to '~/Notes/read-later.md'."},
            },
            "required": [],
        },
    },
    "code_scratchpad": {
        "fn": code_scratchpad.run,
        "description": (
            "Create a new throwaway code file in a chosen language, with a minimal "
            "starter or the clipboard's contents, and open it in an editor. Use it when "
            "the user wants a quick place to try something or to paste a snippet into a "
            "file. The file is only created and opened, never run, and shell scripts are "
            "not offered. Not for a whole "
            "project with a README and folder of its own (new_python_project, "
            "new_node_project, new_web_page)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "enum": [
                        "python", "javascript", "typescript", "html", "css", "json", "sql",
                        "markdown", "text", "swift", "go", "rust", "c", "cpp", "ruby",
                    ],
                    "description": "Language of the file. Defaults to python.",
                },
                "name": {"type": "string", "description": "File name without extension. Defaults to a timestamped name."},
                "folder": {"type": "string", "description": "Folder to create it in. Defaults to '~/Scratch'."},
                "from_clipboard": {"type": "boolean", "description": "Fill the file with the clipboard's text instead of the starter."},
                "open_in": {"type": "string", "description": "Editor to open it in. Defaults to 'Visual Studio Code'."},
            },
            "required": [],
        },
    },
}


# Everything above is code; everything loaded below is data. Captured before
# any generated file is read, so a generated skill can never take one of
# these names -- or a tool's: dispatch() resolves skills before tools, so a
# generated skill named after a tool would silently shadow it (BUGS.md #23).
BUILTIN_SKILLS = frozenset(SKILLS)

MAX_FILE_BYTES = 256 * 1024


def validation_kwargs() -> dict:
    return {
        "tools": TOOLS,
        "reserved_names": set(TOOLS) | set(BUILTIN_SKILLS),
        "skill_names": set(SKILLS),
    }


def _write_atomic(path: Path, definition: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(definition, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _register_definition(definition: dict, path: Path) -> None:
    SKILLS[definition["name"]] = {
        "fn": functools.partial(run_template, definition),
        "description": definition["description"],
        "input_schema": definition["input_schema"],
        "source_signature": definition.get("source_signature"),
        # Everything below is inspection metadata for the Library UI. "origin"
        # is also what makes a skill deletable -- a generated skill is just a
        # JSON file we can remove, a hand-written one is code.
        "origin": "generated",
        "steps": definition["steps"],
        "created_at": definition["created_at"],
        "path": str(path),
        "id": definition["id"],
        "content_hash": definition["content_hash"],
        "version": definition["version"],
        "source": definition["source"],
        "requires": definition["requires"],
    }


def _reject_constant(constant):
    raise SkillValidationError(f"{constant} is not plain JSON data")


def _load_generated_skill(path: Path, seen_ids: set) -> None:
    """Validate one file and register it, migrating a file written before
    metadata existed by writing the computed metadata back. Raises for
    anything that shouldn't load."""
    if path.is_symlink():
        raise SkillValidationError("is a symlink")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise SkillValidationError(f"larger than {MAX_FILE_BYTES} bytes")
    definition = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    if not isinstance(definition, dict):
        raise SkillValidationError("not a JSON object")
    name = definition.get("name")
    if skill_path(GENERATED_DIR, name) != path.resolve():
        raise SkillValidationError(f"file name does not match skill name {name!r}")
    validate.validate_content(definition, **validation_kwargs())

    reasons = []
    missing = validate.METADATA_KEYS - set(definition)
    if missing:
        # created_at falls back to the file's mtime, which is what the
        # Library showed for every generated skill until now.
        fresh = validate.new_metadata(definition, created_at=path.stat().st_mtime)
        definition.update({key: fresh[key] for key in sorted(missing)})
        reasons.append(f"added {', '.join(sorted(missing))}")
    if definition["content_hash"] != validate.content_hash(definition) or (
        definition["requires"] != validate.requires(definition)
    ):
        if definition.get("source") != "learned":
            # An imported skill's hash is its provenance claim; content that
            # no longer matches it isn't that skill any more.
            raise SkillValidationError("content does not match content_hash")
        # A learned skill edited by hand on this machine: a new revision.
        definition["content_hash"] = validate.content_hash(definition)
        definition["requires"] = validate.requires(definition)
        if isinstance(definition.get("version"), int) and not isinstance(definition["version"], bool):
            definition["version"] += 1
        reasons.append("content changed on disk")
    validate.validate_metadata(definition)
    if definition["id"] in seen_ids:
        raise SkillValidationError(f"duplicate id {definition['id']}")

    if reasons:
        _write_atomic(path, definition)
        log_event("skill_migrated", name=name, path=str(path), reasons=reasons)
    seen_ids.add(definition["id"])
    _register_definition(definition, path)


def load_generated_skills() -> None:
    """(Re)load every generated skill from GENERATED_DIR. A file that fails
    validation is logged as skill_load_rejected and skipped -- one bad file
    never stops startup or the other skills loading."""
    for name in [n for n, s in SKILLS.items() if s.get("origin") == "generated"]:
        SKILLS.pop(name)
    if not GENERATED_DIR.exists():
        return
    seen_ids: set = set()
    for path in sorted(GENERATED_DIR.glob("*.json")):
        try:
            _load_generated_skill(path, seen_ids)
        except (SkillValidationError, OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            log_event("skill_load_rejected", path=str(path), reason=str(e) or type(e).__name__)


load_generated_skills()


def available_name(name: str) -> str:
    """name, or name_2, name_3... -- the first that no tool, skill, or file
    on disk already has. An invalid name comes back unchanged, so the
    validator reports it rather than this papering over it."""
    try:
        validate.check_name(name)
    except SkillValidationError:
        return name
    candidate, n = name, 1
    while candidate in SKILLS or candidate in TOOLS or (GENERATED_DIR / f"{candidate}.json").exists():
        n += 1
        suffix = f"_{n}"
        candidate = name[: 64 - len(suffix)] + suffix
    return candidate


def register_skill(definition: dict) -> dict:
    """Validate a newly-approved generated skill, stamp its metadata, write
    it, and make it usable in this running process with no restart. Returns
    {"ok": False, "error"} instead of raising; nothing is written unless the
    whole definition validates."""
    try:
        path = skill_path(GENERATED_DIR, definition.get("name"))
        content = {k: v for k, v in definition.items() if k not in validate.METADATA_KEYS}
        validate.validate_content(content, **validation_kwargs())
        full = {**content, **validate.new_metadata(content, created_at=time.time())}
        validate.validate(full, **validation_kwargs())
    except (SkillValidationError, KeyError, TypeError, AttributeError, ValueError) as e:
        return {"ok": False, "error": str(e)}

    existing = SKILLS.get(full["name"])
    if existing is not None:
        if existing.get("origin") == "generated" and existing.get("content_hash") == full["content_hash"]:
            return {"ok": True, "name": full["name"], "id": existing["id"], "unchanged": True}
        return {"ok": False, "error": f"a skill named {full['name']!r} already exists"}
    if path.exists() or path.is_symlink():
        # Most likely a file that was rejected at load; don't clobber it.
        return {"ok": False, "error": f"{path.name} already exists on disk"}

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, full)
    _register_definition(full, path)
    return {"ok": True, "name": full["name"], "id": full["id"], "content_hash": full["content_hash"]}


def delete_skill(name: str) -> dict:
    """Remove a generated skill for good: unregister it, delete its JSON file,
    and drop any Library overrides attached to its name. Hand-written skills
    are code, not data -- they can be disabled, but not deleted from here."""
    from agent.core import preferences

    skill = SKILLS.get(name)
    if skill is None:
        return {"ok": False, "error": f"no such skill: {name}"}
    if skill.get("origin") != "generated":
        return {"ok": False, "error": f"{name} is a built-in skill and can only be disabled"}

    path = Path(skill["path"])
    if path.exists():
        path.unlink()
    SKILLS.pop(name, None)
    preferences.clear(name)
    return {"ok": True, "name": name}


def anthropic_skill_schemas() -> list:
    """Only enabled skills are described to the model -- see the matching note
    in tools/registry.anthropic_tool_schemas()."""
    from agent.core import preferences

    return [
        {"name": name, "description": skill["description"], "input_schema": skill["input_schema"]}
        for name, skill in SKILLS.items()
        if preferences.is_enabled(name)
    ]
