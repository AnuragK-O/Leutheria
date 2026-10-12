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

# --- files ---
from agent.tools.extract_archive import extract_archive
from agent.tools.file_info import file_info
from agent.tools.find_duplicates import find_duplicates
from agent.tools.find_files import find_files
from agent.tools.folder_size import folder_size
from agent.tools.search_file_contents import search_file_contents
from agent.tools.spotlight_search import spotlight_search
from agent.tools.write_new_file import write_new_file
from agent.tools.zip_folder import zip_folder

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
    # --- files ---
    # All "safe" by construction, each for a stated reason:
    #   find_files, file_info, folder_size, find_duplicates,
    #   search_file_contents, spotlight_search -- read-only, never follow
    #       symlinks, bounded in time, entries and output.
    #   write_new_file -- exclusive create (open mode "x"): the existence
    #       check and the create are one atomic step, so it cannot replace
    #       or truncate anything.
    #   zip_folder -- reads the source, creates the archive exclusively.
    #   extract_archive -- only ever writes inside a folder it created
    #       itself with an exclusive mkdir; entry names are validated before
    #       anything is written (zip-slip); links are never created.
    "find_files": {
        "fn": find_files,
        "safety": "safe",
        "description": (
            "Search inside one folder for files by name, type, size or age, and get back their "
            "paths, sizes and modified times. Use this when the user asks where a file is in a "
            "folder they name or imply, such as Downloads, the Desktop or a project, or wants "
            "the biggest, newest or oldest files there. It looks through subfolders unless "
            "recursive is false, skips hidden files, and only reads. Do not use it to find a "
            "file anywhere on the Mac when the folder is unknown, which is spotlight_search, or "
            "to search the words inside files, which is search_file_contents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder to search, e.g. '~/Downloads'."},
                "name": {
                    "type": "string",
                    "description": "Part of the file name, case-insensitive, e.g. 'invoice'. May use * and ? wildcards.",
                },
                "extension": {
                    "type": "string",
                    "description": "File type without the dot, e.g. 'pdf'. Several may be given separated by commas, e.g. 'jpg,png'.",
                },
                "min_size_mb": {"type": "number", "description": "Only files at least this many megabytes."},
                "modified_within_days": {
                    "type": "number",
                    "description": "Only files changed in the last this many days. Use 1 for today, 2 to include yesterday.",
                },
                "older_than_days": {
                    "type": "number",
                    "description": "Only files not changed for at least this many days.",
                },
                "recursive": {"type": "boolean", "description": "Look inside subfolders too. Defaults to true."},
                "sort": {
                    "type": "string",
                    "enum": ["modified", "oldest", "size", "name"],
                    "description": "Order of results: newest first (the default), oldest first, largest first, or by name.",
                },
                "limit": {"type": "integer", "description": "Most results to return. Defaults to 50, at most 500."},
            },
            "required": ["path"],
        },
    },
    "file_info": {
        "fn": file_info,
        "safety": "safe",
        "description": (
            "Get the facts about one file or folder: whether it exists, whether it is a file or "
            "a folder, its size, and when it was created and last modified. Use this to check a "
            "path before acting on it or to answer how big or how old one named file is. A path "
            "that does not exist is reported as exists false, not as an error. For a folder it "
            "counts the items directly inside but does not add up their sizes; use folder_size "
            "for how much space a folder takes. It does not read a file's contents; use "
            "read_file for that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path of the file or folder to look at."}},
            "required": ["path"],
        },
    },
    "folder_size": {
        "fn": folder_size,
        "safety": "safe",
        "description": (
            "Measure how much space a folder takes in total and list the largest items directly "
            "inside it, where an item is a file or a whole subfolder. Use this for questions "
            "like what is taking up space in a folder. It only reads. On a very large folder "
            "the answer is a lower bound and incomplete is true. For free space on the whole "
            "disk use get_disk_space instead, and for the largest individual files at any depth "
            "use find_files sorted by size."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder to measure, e.g. '~/Downloads'."},
                "limit": {
                    "type": "integer",
                    "description": "How many of the largest items to list. Defaults to 10, at most 50.",
                },
            },
            "required": ["path"],
        },
    },
    "zip_folder": {
        "fn": zip_folder,
        "safety": "safe",
        "description": (
            "Compress a folder, or a single file, into a new zip file. The original is left "
            "exactly as it was. It refuses if a file already exists at the destination rather "
            "than replacing it, and it leaves out symbolic links. Use this when you need to "
            "choose the exact zip path or leave certain subfolders out. For a plain request to "
            "zip a folder, prefer the archive_folder skill, which also picks a free name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Folder or file to compress."},
                "destination": {
                    "type": "string",
                    "description": "Path of the zip file to create, or a folder to put it in. Defaults to the source's name with .zip, next to the source.",
                },
                "exclude": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of files or subfolders to leave out wherever they appear, e.g. 'node_modules'.",
                },
            },
            "required": ["source"],
        },
    },
    "extract_archive": {
        "fn": extract_archive,
        "safety": "safe",
        "description": (
            "Unpack a zip or tar archive, including tar.gz, into a brand new folder. The "
            "archive itself is kept. It refuses if the destination already exists, so it never "
            "mixes files into an existing folder or replaces anything, and it refuses an "
            "archive whose entries try to write outside that folder. Use this when the user "
            "names the archive to unzip or extract. Other formats such as rar, 7z and dmg are "
            "not supported."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "archive": {"type": "string", "description": "Path of the zip or tar file."},
                "destination": {
                    "type": "string",
                    "description": "Folder to create and unpack into. It must not exist yet. Defaults to a folder named after the archive, next to it.",
                },
            },
            "required": ["archive"],
        },
    },
    "find_duplicates": {
        "fn": find_duplicates,
        "safety": "safe",
        "description": (
            "Find files inside a folder that are exact copies of each other, compared by their "
            "content rather than their names, and report each set with how much space the "
            "extra copies take. It only reports; it never moves or removes anything, and you "
            "should not remove any copy unless the user says which to keep. If the user wants "
            "the list written down to look through, use the duplicate_report skill instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder to check, e.g. '~/Pictures'."},
                "min_size_kb": {
                    "type": "number",
                    "description": "Ignore files smaller than this many kilobytes. Defaults to 1.",
                },
                "recursive": {"type": "boolean", "description": "Look inside subfolders too. Defaults to true."},
                "limit": {
                    "type": "integer",
                    "description": "Most sets to return, largest waste first. Defaults to 20, at most 50.",
                },
            },
            "required": ["path"],
        },
    },
    "write_new_file": {
        "fn": write_new_file,
        "safety": "safe",
        "description": (
            "Create a new text file with the given content, making any missing parent folders. "
            "It only ever creates: if anything already exists at that path it refuses and "
            "changes nothing, so it can never replace or erase a file. Use this to save text, "
            "notes, code or a document as a file the user asked for. To add to a file that "
            "already exists use append_text. Changing or replacing the existing content of a "
            "file is not something this tool can do; if it refuses, pick a different name or "
            "ask the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of the file to create, e.g. '~/Documents/plan.md'."},
                "content": {"type": "string", "description": "The full text to put in the file."},
            },
            "required": ["path", "content"],
        },
    },
    "search_file_contents": {
        "fn": search_file_contents,
        "safety": "safe",
        "description": (
            "Find which text files in a folder contain a word or phrase, and show the matching "
            "lines. Use this when the user remembers what a file says but not what it is "
            "called, for example which note mentions the wifi password. The match is "
            "case-insensitive and literal. It only reads plain text files such as notes, "
            "Markdown, code and CSV; it cannot see inside PDFs, Word documents or images. To "
            "find a file by its name use find_files or spotlight_search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder to search, or a single file."},
                "text": {"type": "string", "description": "The word or phrase to look for."},
                "extension": {
                    "type": "string",
                    "description": "Only search files of this type, without the dot, e.g. 'md'. Several may be given separated by commas.",
                },
                "limit": {"type": "integer", "description": "Most files to return. Defaults to 20, at most 100."},
            },
            "required": ["path", "text"],
        },
    },
    "spotlight_search": {
        "fn": spotlight_search,
        "safety": "safe",
        "description": (
            "Find files and folders by name anywhere on the Mac using the Spotlight index, most "
            "recently modified first. Use this when the user asks where something is and you do "
            "not know which folder it is in, for example find my resume. It matches names only, "
            "not the text inside files, and only what Spotlight has indexed. When the folder is "
            "known, or you need to filter by size or age, use find_files instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The file name or part of it, e.g. 'resume'."},
                "folder": {"type": "string", "description": "Only look inside this folder. Optional."},
                "limit": {"type": "integer", "description": "Most results to return. Defaults to 20, at most 50."},
            },
            "required": ["query"],
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
