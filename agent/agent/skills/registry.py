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
