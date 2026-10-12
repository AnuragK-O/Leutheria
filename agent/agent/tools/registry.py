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

# --- system ---
from agent.tools.cancel_timer import cancel_timer
from agent.tools.get_network_status import get_network_status
from agent.tools.get_system_info import get_system_info
from agent.tools.get_volume import get_volume
from agent.tools.get_weather import get_weather
from agent.tools.list_timers import list_timers
from agent.tools.media_control import media_control
from agent.tools.open_file import open_file
from agent.tools.reveal_in_finder import reveal_in_finder
from agent.tools.set_home_location import set_home_location
from agent.tools.set_timer import set_timer
from agent.tools.set_volume import set_volume

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
    # --- system ---
    # All "safe", each for a stated structural reason rather than by default:
    # the get_*/list_* tools only read; set_volume is reversible and returns
    # the previous state; media_control can send three fixed media keys and
    # nothing else; timers hold no data and only notify and speak;
    # set_home_location replaces one setting of Leutheria's own and returns
    # the previous value; reveal_in_finder only selects; open_file is an
    # allowlist that refuses anything "opening" would execute (see the module).
    "get_volume": {
        "fn": get_volume,
        "safety": "safe",
        "description": (
            "Read the Mac's sound output volume, from 0 to 100, and whether it is muted. "
            "Use this when the user asks how loud it is. To change the volume use "
            "set_volume, which already reports the level before and after."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "set_volume": {
        "fn": set_volume,
        "safety": "safe",
        "description": (
            "Change the Mac's sound output volume or mute it. Use level for an exact "
            "setting such as 'volume to 30', change for a relative one such as 'turn it "
            "down' (about minus 15) or 'a bit louder' (about plus 10), and muted to mute "
            "or unmute. Changing the level never unmutes by itself, so pass muted false "
            "as well when the user wants to hear something. This is the system volume, "
            "not the volume inside one app, and it does not pause anything; to pause "
            "music use media_control."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "level": {"type": "integer", "description": "Exact volume from 0 to 100."},
                "change": {
                    "type": "integer",
                    "description": "Amount to add to the current volume, negative to lower it. Not together with level.",
                },
                "muted": {"type": "boolean", "description": "True to mute, false to unmute."},
            },
            "required": [],
        },
    },
    "media_control": {
        "fn": media_control,
        "safety": "safe",
        "description": (
            "Press one of the keyboard's media keys: play_pause, next or previous. It acts "
            "on whatever macOS treats as the current player, such as Music, Spotify or a "
            "browser tab. Play and pause are one toggle and there is no way to check "
            "whether anything is playing, so say that you pressed play/pause rather than "
            "claiming the music stopped or started. If nothing has been playing, macOS may "
            "open the Music app. Do not use this to change loudness; that is set_volume."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play_pause", "next", "previous"],
                    "description": "Which media key to press.",
                }
            },
            "required": ["action"],
        },
    },
    "get_system_info": {
        "fn": get_system_info,
        "safety": "safe",
        "description": (
            "Get how the Mac itself is doing: processor load, memory used and available, "
            "how long since it was restarted, the macOS version and the model. Use this "
            "for questions like 'is my Mac busy' or 'how much memory is free'. It does not "
            "cover the battery, disk space or network, which have their own tools; for an "
            "overall check of everything at once use system_health_check."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "get_network_status": {
        "fn": get_network_status,
        "safety": "safe",
        "description": (
            "Check whether the Mac is connected to a network and whether the internet is "
            "reachable, with the connection type such as Wi-Fi or Ethernet, the local IP "
            "address and the router address. Use it for 'am I online' or 'what is my IP'. "
            "It reports the local address only, not the public one, and not the Wi-Fi "
            "network's name or the connection speed."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "get_weather": {
        "fn": get_weather,
        "safety": "safe",
        "description": (
            "Get the current weather and today's forecast for a place, from the Open-Meteo "
            "service. Give place as a city, optionally with a region or country after a "
            "comma, such as 'Austin, Texas'. Leave place out to use the saved home "
            "location; if none is saved the call fails and you should ask the user which "
            "city they mean rather than guessing. Set days up to 7 to also get daily "
            "forecasts. Temperatures follow the Mac's region unless units is given. Needs "
            "an internet connection. For a spoken outlook over several days use "
            "weather_report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": "City name, e.g. 'Paris' or 'Portland, Oregon'. Omit for home."},
                "days": {"type": "integer", "description": "How many days of forecast, 1 to 7. Defaults to 1 (today)."},
                "units": {
                    "type": "string",
                    "enum": ["metric", "imperial"],
                    "description": "Celsius and km/h, or Fahrenheit and mph. Omit to follow the Mac's region.",
                },
            },
            "required": [],
        },
    },
    "set_home_location": {
        "fn": set_home_location,
        "safety": "safe",
        "description": (
            "Remember a city as the user's home location, so weather requests that name no "
            "place use it. Use this only when the user tells you where they live or asks "
            "you to remember their city, never on a guess and never just because they "
            "asked about the weather somewhere. It replaces any location saved before and "
            "reports what the previous one was."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": "City name, optionally 'City, Region' or 'City, Country'."}
            },
            "required": ["place"],
        },
    },
    "set_timer": {
        "fn": set_timer,
        "safety": "safe",
        "description": (
            "Start a countdown timer that shows a notification and speaks a line when it "
            "ends. Give the duration in seconds, minutes or hours; they add together, so "
            "an hour and a half is hours 1 and minutes 30. Add a short label when the user "
            "names what it is for, such as 'pasta', and optionally a message to be spoken "
            "when it ends, which makes it a reminder, such as 'Time to take the pizza out'. "
            "Timers run inside Leutheria and are lost if it quits or restarts, so this is "
            "for minutes or hours, at most 24 hours, and at most 20 at once; it is not an "
            "alarm for a clock time and not a calendar reminder. For a plain spoken "
            "request like 'set a timer for ten minutes', quick_timer does this and "
            "confirms on screen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "description": "Seconds to count down."},
                "minutes": {"type": "number", "description": "Minutes to count down."},
                "hours": {"type": "number", "description": "Hours to count down."},
                "label": {"type": "string", "description": "Short name for the timer, e.g. 'pasta'."},
                "message": {"type": "string", "description": "Sentence to speak when it ends, instead of the default."},
            },
            "required": [],
        },
    },
    "list_timers": {
        "fn": list_timers,
        "safety": "safe",
        "description": (
            "List the timers that are running, soonest first, with how long each has left "
            "and its id. Use this for 'how long is left' or before cancelling when more "
            "than one could be meant. Only timers set in this run of Leutheria exist; a "
            "restart clears them."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "cancel_timer": {
        "fn": cancel_timer,
        "safety": "safe",
        "description": (
            "Cancel a running timer. Pass timer_id or label to pick one, all true to cancel "
            "every timer, or nothing at all when exactly one timer is running. If several "
            "are running and it is not clear which the user means, ask or call list_timers "
            "instead of cancelling all of them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "timer_id": {"type": "string", "description": "The id from set_timer or list_timers."},
                "label": {"type": "string", "description": "The timer's label, or part of it."},
                "all": {"type": "boolean", "description": "True to cancel every running timer."},
            },
            "required": [],
        },
    },
    "open_file": {
        "fn": open_file,
        "safety": "safe",
        "description": (
            "Open a document, image, audio or video file, or a folder, in the app the Mac "
            "normally uses for it, as if the user had double-clicked it. It refuses "
            "applications, scripts, programs, installers and anything it cannot identify, "
            "because opening those would run them; use open_app for an application and "
            "reveal_in_finder to just show a file. To read a text file's contents "
            "yourself use read_file instead; this only puts it on screen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of the file or folder, e.g. '~/Documents/resume.pdf'."}
            },
            "required": ["path"],
        },
    },
    "reveal_in_finder": {
        "fn": reveal_in_finder,
        "safety": "safe",
        "description": (
            "Show a file or folder in a Finder window with it selected, without opening "
            "it. Use this for 'show me where that is' or when a file should be visible but "
            "not opened. Works for any kind of file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of the file or folder to show."}
            },
            "required": ["path"],
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
