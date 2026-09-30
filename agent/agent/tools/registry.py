from agent.tools.append_text import append_text
from agent.tools.copy_file import copy_file
from agent.tools.create_folder import create_folder
from agent.tools.describe_ui import describe_ui
from agent.tools.focus_app import focus_app
from agent.tools.get_battery_status import get_battery_status
from agent.tools.get_clipboard import get_clipboard
from agent.tools.get_current_datetime import get_current_datetime
from agent.tools.get_disk_space import get_disk_space
from agent.tools.list_files import list_files
from agent.tools.list_running_apps import list_running_apps
from agent.tools.move_file import move_file
from agent.tools.open_app import open_app
from agent.tools.open_url import open_url
from agent.tools.press_keys import press_keys
from agent.tools.read_file import read_file
from agent.tools.run_command import run_command
from agent.tools.set_clipboard import set_clipboard
from agent.tools.show_notification import show_notification
from agent.tools.take_screenshot import take_screenshot
from agent.tools.trash_file import trash_file
from agent.tools.type_text import type_text

# run_command is marked destructive, not "safe", because it executes
# arbitrary shell input -- unlike the others, its blast radius isn't bounded
# by fixed parameters. A destructive tool pauses for a live user confirmation
# before it runs (see dispatcher.dispatch/_confirm). Every other tool here is
# "safe" by construction, not by omission: trash_file uses the system Trash
# (recoverable, unlike rm), move_file/copy_file refuse to overwrite an
# existing destination, append_text can only add content, never truncate or
# overwrite. That's what lets them skip the confirmation flow.
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
        "description": "Run an arbitrary shell command. Requires the user to explicitly approve it before it runs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "The shell command to run."}
            },
            "required": ["cmd"],
        },
    },
    "get_clipboard": {
        "fn": get_clipboard,
        "safety": "safe",
        "description": "Read the current contents of the system clipboard.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "set_clipboard": {
        "fn": set_clipboard,
        "safety": "safe",
        "description": "Copy text to the system clipboard.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The text to copy to the clipboard."}},
            "required": ["text"],
        },
    },
    "open_url": {
        "fn": open_url,
        "safety": "safe",
        "description": "Open a URL in the default web browser.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to open, e.g. 'https://u.gg'."}
            },
            "required": ["url"],
        },
    },
    "show_notification": {
        "fn": show_notification,
        "safety": "safe",
        "description": "Show a macOS notification banner.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Notification title."},
                "message": {"type": "string", "description": "Notification body text."},
            },
            "required": ["title", "message"],
        },
    },
    "take_screenshot": {
        "fn": take_screenshot,
        "safety": "safe",
        "description": "Take a screenshot of the whole screen and save it as a PNG file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "save_path": {
                    "type": "string",
                    "description": "Where to save the screenshot. Defaults to a timestamped file on the Desktop if omitted.",
                }
            },
            "required": [],
        },
    },
    "read_file": {
        "fn": read_file,
        "safety": "safe",
        "description": "Read the text contents of a file (capped at 20,000 characters).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path of the file to read."}},
            "required": ["path"],
        },
    },
    "append_text": {
        "fn": append_text,
        "safety": "safe",
        "description": "Append a line of text to a file, creating the file (and any missing parent folders) if it doesn't exist. Never overwrites existing content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of the file to append to."},
                "text": {"type": "string", "description": "The text to append as a new line."},
            },
            "required": ["path", "text"],
        },
    },
    "trash_file": {
        "fn": trash_file,
        "safety": "safe",
        "description": "Move a file or folder to the system Trash. Recoverable -- not a permanent delete.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path of the file or folder to trash."}},
            "required": ["path"],
        },
    },
    "move_file": {
        "fn": move_file,
        "safety": "safe",
        "description": "Move or rename a file or folder. Refuses if the destination already exists, rather than overwriting it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Path of the file or folder to move."},
                "destination": {"type": "string", "description": "Where to move it to."},
            },
            "required": ["source", "destination"],
        },
    },
    "copy_file": {
        "fn": copy_file,
        "safety": "safe",
        "description": "Copy a file or folder. Refuses if the destination already exists, rather than overwriting it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Path of the file or folder to copy."},
                "destination": {"type": "string", "description": "Where to copy it to."},
            },
            "required": ["source", "destination"],
        },
    },
    "list_running_apps": {
        "fn": list_running_apps,
        "safety": "safe",
        "description": "List the names of currently running, visible applications.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "get_battery_status": {
        "fn": get_battery_status,
        "safety": "safe",
        "description": "Get the current battery percentage and charging state (if this Mac has a battery).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "get_disk_space": {
        "fn": get_disk_space,
        "safety": "safe",
        "description": "Get total, used, and free disk space (in GB) for the main disk.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "get_current_datetime": {
        "fn": get_current_datetime,
        "safety": "safe",
        "description": "Get the current date and time.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "focus_app": {
        "fn": focus_app,
        "safety": "scoped",
        "target": "name",
        "description": (
            "Bring an application to the front and make it the working context for "
            "what follows. Use this before typing into an app. Unlike open_app this "
            "waits until the app is really frontmost, and it's remembered, so later "
            "requests can refer to 'the note' or 'the document' without naming the app."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The application name, e.g. 'TextEdit'."}
            },
            "required": ["name"],
        },
    },
    "describe_ui": {
        "fn": describe_ui,
        "safety": "safe",
        "description": (
            "Read the on-screen structure of an app's window: its text areas, fields "
            "and buttons, which one has keyboard focus, and what they contain. Use "
            "this to check where typing will land before typing, the same way you'd "
            "list a folder before writing into it. Password fields are never returned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "App to inspect. Defaults to the current working app, or whatever is frontmost.",
                }
            },
            "required": [],
        },
    },
    "press_keys": {
        "fn": press_keys,
        "safety": "scoped",
        "target": "frontmost",
        "description": (
            "Send a keyboard shortcut to the frontmost app, e.g. 'cmd+n' for a new "
            "document, 'cmd+s' to save, 'cmd+z' to undo, or 'return'/'tab'/'escape'. "
            "Use this for app commands that have no text equivalent, rather than "
            "shelling out to osascript. One shortcut per call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "string",
                    "description": "Shortcut in 'modifier+key' form, e.g. 'cmd+n' or 'cmd+shift+s'.",
                }
            },
            "required": ["keys"],
        },
    },
    "type_text": {
        "fn": type_text,
        "safety": "scoped",
        "target": "frontmost",
        "description": (
            "Type text into whatever currently has keyboard focus, as if the user typed "
            "it. Text is inserted at the cursor, so to add to a document that already "
            "has content, send only the new text -- do not retype what is already there. "
            "Newlines in the text are sent as real Return keypresses. Focus the right "
            "app first, and use describe_ui to check where the cursor is if unsure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text to type."}
            },
            "required": ["text"],
        },
    },
}


def anthropic_tool_schemas() -> list:
    """Only enabled tools are described to the model -- a tool switched off in
    the Library is invisible to it, not merely refused after the fact."""
    from agent.core import preferences

    return [
        {"name": name, "description": tool["description"], "input_schema": tool["input_schema"]}
        for name, tool in TOOLS.items()
        if preferences.is_enabled(name)
    ]
