from agent.skills import setup_project

# A skill is a hand-written, multi-step procedure exposed to the LLM as if
# it were a single tool. Its "fn" gets (args, websocket, pending) instead of
# **args, since it needs to call back into dispatch() for its own sub-steps
# (so a skill's destructive actions still go through the normal per-step
# confirmation flow -- a skill isn't a way to bypass safety tiering, just a
# way to avoid re-deriving the same multi-step plan via LLM reasoning every
# time).
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
}


def anthropic_skill_schemas() -> list:
    return [
        {"name": name, "description": skill["description"], "input_schema": skill["input_schema"]}
        for name, skill in SKILLS.items()
    ]
