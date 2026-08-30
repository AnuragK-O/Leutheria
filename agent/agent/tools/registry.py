from agent.tools.basic import create_folder, list_files, open_app, run_command

# "safety" is a stub for the confirmation-tiering work (roadmap step 5).
# run_command is marked destructive now, not "safe", because it executes
# arbitrary shell input -- unlike the other three, its blast radius isn't
# bounded by fixed parameters.
TOOLS = {
    "open_app": {
        "fn": open_app,
        "safety": "safe",
        "description": "Open a macOS application by its name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The application name, e.g. 'Calculator' or 'Safari'."}
            },
            "required": ["name"],
        },
    },
    "create_folder": {
        "fn": create_folder,
        "safety": "safe",
        "description": "Create a folder (and any missing parent folders) at the given path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Filesystem path of the folder to create, e.g. '~/Desktop/notes'."}
            },
            "required": ["path"],
        },
    },
    "list_files": {
        "fn": list_files,
        "safety": "safe",
        "description": "List the names of files and folders inside a given directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to list, e.g. '~/Desktop'."}
            },
            "required": ["path"],
        },
    },
    "run_command": {
        "fn": run_command,
        "safety": "destructive",
        "description": "Run an arbitrary shell command. Currently blocked from execution pending confirmation UX.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "The shell command to run."}
            },
            "required": ["cmd"],
        },
    },
}


def anthropic_tool_schemas() -> list:
    return [
        {"name": name, "description": tool["description"], "input_schema": tool["input_schema"]}
        for name, tool in TOOLS.items()
    ]
