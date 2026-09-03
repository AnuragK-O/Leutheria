import functools
import json
from pathlib import Path

from agent.skills import backup_folder, quick_note, setup_project
from agent.skills.template_skill import run_template

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
            "Jot down a quick timestamped note and confirm it with a notification. "
            "Use this whenever the user wants to remember or note something down."
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


def _load_generated_skill(path: Path) -> None:
    definition = json.loads(path.read_text())
    SKILLS[definition["name"]] = {
        "fn": functools.partial(run_template, definition),
        "description": definition["description"],
        "input_schema": definition["input_schema"],
        "source_signature": definition.get("source_signature"),
    }


def _load_all_generated_skills() -> None:
    if not GENERATED_DIR.exists():
        return
    for path in sorted(GENERATED_DIR.glob("*.json")):
        try:
            _load_generated_skill(path)
        except Exception:
            continue  # a malformed generated-skill file shouldn't break startup


_load_all_generated_skills()


def register_skill(definition: dict) -> None:
    """Persist a newly-approved generated skill to disk and make it usable
    immediately in this running process, no restart needed."""
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = GENERATED_DIR / f"{definition['name']}.json"
    path.write_text(json.dumps(definition, indent=2))
    _load_generated_skill(path)


def anthropic_skill_schemas() -> list:
    return [
        {"name": name, "description": skill["description"], "input_schema": skill["input_schema"]}
        for name, skill in SKILLS.items()
    ]
