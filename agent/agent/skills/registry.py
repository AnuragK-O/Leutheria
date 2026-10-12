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

# --- system ---
from agent.skills import (
    battery_check,
    end_of_day,
    focus_mode,
    meeting_prep,
    morning_briefing,
    pomodoro,
    quick_timer,
    system_health_check,
    weather_report,
    wind_down,
)

# --- files ---
from agent.skills import (
    archive_folder,
    archive_old_downloads,
    clean_desktop,
    collect_screenshots,
    daily_journal,
    duplicate_report,
    extract_latest_download,
    find_large_files,
    find_recent_files,
    folder_summary,
    organize_by_date,
    organize_folder,
    project_snapshot,
    screenshot_to_folder,
    storage_report,
)
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
    # --- system ---
    "morning_briefing": {
        "fn": morning_briefing.run,
        "description": (
            "Give a spoken start-of-day summary: the date and time, the battery, low disk "
            "space if any, and the current weather with today's forecast. Use it when the "
            "user asks for their briefing or how the day looks. Pass place only if they "
            "name a city; otherwise the saved home location is used. It does not read "
            "calendars, reminders, mail or news, so do not use it for 'what's on my "
            "schedule'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": "City for the weather, only if the user named one."}
            },
            "required": [],
        },
    },
    "system_health_check": {
        "fn": system_health_check.run,
        "description": (
            "Check the Mac's overall health in one go: processor load, memory, disk space, "
            "battery and network, and report either that all is well or which things need "
            "attention. Use it for broad questions like 'how's my Mac doing' or 'is "
            "everything OK with my computer'. For one specific figure, such as just the "
            "battery or just free disk space, call that single tool instead."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "weather_report": {
        "fn": weather_report.run,
        "description": (
            "Give a day-by-day weather outlook for the next 2 to 7 days, 3 unless told "
            "otherwise. Use it for 'what's the weather this week' or 'will it rain this "
            "weekend'. Pass place only if the user names one; otherwise the saved home "
            "location is used. For the weather right now or just today, call get_weather "
            "instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": "City, only if the user named one."},
                "days": {"type": "integer", "description": "How many days to cover, 2 to 7. Defaults to 3."},
            },
            "required": [],
        },
    },
    "quick_timer": {
        "fn": quick_timer.run,
        "description": (
            "Start a timer from a duration said in words, such as '10 minutes', '90 "
            "seconds' or '1 hour 30 minutes', and show a banner on screen confirming when "
            "it will end. Use it for an ordinary 'set a timer for ...' request. For a "
            "reminder that should say something specific when it ends, or when a timer is "
            "one step of something larger, call set_timer instead. Do not use it for a "
            "focus session with a break; that is pomodoro."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "duration": {"type": "string", "description": "The length in words or digits, e.g. '10 minutes' or '1h30m'."},
                "label": {"type": "string", "description": "Short name if the user gave one, e.g. 'tea'."},
            },
            "required": ["duration"],
        },
    },
    "pomodoro": {
        "fn": pomodoro.run,
        "description": (
            "Start one pomodoro work cycle: a focus timer, 25 minutes unless told "
            "otherwise, followed by a break timer, 5 minutes, each announced when it ends. "
            "Use it when the user says pomodoro or asks for a work session with a break. "
            "It only sets timers; it does not mute sound or block anything. For a single "
            "plain timer use quick_timer, and to silence the Mac while working use "
            "focus_mode."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "focus_minutes": {"type": "number", "description": "Length of the focus part. Defaults to 25."},
                "break_minutes": {"type": "number", "description": "Length of the break. Defaults to 5."},
                "task": {"type": "string", "description": "What the user is working on, if they said."},
            },
            "required": [],
        },
    },
    "focus_mode": {
        "fn": focus_mode.run,
        "description": (
            "Mute the Mac's sound and start a focus timer, 25 minutes by default, for when "
            "the user wants quiet to concentrate. It does not turn on the macOS Do Not "
            "Disturb or Focus setting and does not silence notification banners, so say so "
            "rather than promising no interruptions. Sound stays muted afterwards until "
            "the user asks to unmute. Use pomodoro instead if they want a timed break, and "
            "meeting_prep if they are going into a meeting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "number", "description": "How long to focus. Defaults to 25."}
            },
            "required": [],
        },
    },
    "meeting_prep": {
        "fn": meeting_prep.run,
        "description": (
            "Get the Mac ready for a meeting. For kind call, a video or audio call on this "
            "Mac, it checks the network and battery and makes sure sound is on. For kind "
            "in_person, where the Mac should stay quiet, it mutes the sound and checks the "
            "battery. Optionally opens a named app such as Zoom and sets a timer for the "
            "meeting's length. Use it when the user says they are heading into or about to "
            "join a meeting. It cannot join the call, read the calendar, or mute the "
            "microphone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["call", "in_person"],
                    "description": "call for a meeting on this Mac (the default), in_person for one away from it.",
                },
                "app": {"type": "string", "description": "App to open for the meeting, e.g. 'Zoom'. Omit to open nothing."},
                "minutes": {"type": "number", "description": "Meeting length, to be told when time is up. Omit for no timer."},
            },
            "required": [],
        },
    },
    "end_of_day": {
        "fn": end_of_day.run,
        "description": (
            "Give a spoken end-of-day wrap-up: the time, any timers still running, the "
            "battery, low disk space, whether a restart is due, and tomorrow's forecast if "
            "a home location is saved. Use it when the user says they are done for the day "
            "or wrapping up. It only reports and changes nothing; it does not close apps, "
            "save work, or lower the volume. To get the Mac quiet for the evening use "
            "wind_down."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "wind_down": {
        "fn": wind_down.run,
        "description": (
            "Lower the Mac's volume to a quiet level, 20 unless told otherwise, and set a "
            "timer, 30 minutes by default, that nudges the user to stop for the night. Use "
            "it when the user says they are winding down or getting ready for bed. It "
            "never raises the volume, and it does not dim the screen, turn on Night Shift "
            "or put the Mac to sleep."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "number", "description": "Minutes until the nudge. Defaults to 30."},
                "volume": {"type": "integer", "description": "Volume to lower to, 0 to 100. Defaults to 20."},
            },
            "required": [],
        },
    },
    "battery_check": {
        "fn": battery_check.run,
        "description": (
            "Check the battery and say whether it needs attention, putting a warning "
            "banner on screen if it is at or below the threshold, 20 percent by default, "
            "and not charging. Use it for 'do I need to plug in' or 'will my battery be "
            "OK'. For just the percentage, call get_battery_status instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "threshold": {"type": "integer", "description": "Percent at or below which to warn. Defaults to 20."}
            },
            "required": [],
        },
    },
    # --- files ---
    # Every step of every skill below goes through dispatch() (see
    # files_common.StepLog); none imports a tool function. The five that
    # move files in bulk share files_common.move_into_folders(): move_file
    # and create_folder only, never overwrite, never delete, a per-run cap,
    # and a dry_run that performs reads only.
    "organize_folder": {
        "fn": organize_folder.run,
        "description": (
            "Sort the loose files at the top of a folder the user names into subfolders by "
            "type, such as Images, Documents and Archives, inside that same folder. Files of "
            "unknown type, subfolders and hidden files are left alone, nothing is ever "
            "replaced or deleted, and at most 200 files move per run. Not for the Desktop, "
            "which is clean_desktop, not for sorting by date, which is organize_by_date, and "
            "it refuses code projects and system folders."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The folder to organise, e.g. '~/Downloads'."},
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, only report what would be moved and move nothing. Use it when the user asks what would happen or wants to check first.",
                },
            },
            "required": ["path"],
        },
    },
    "organize_by_date": {
        "fn": organize_by_date.run,
        "description": (
            "Sort the loose files at the top of a folder into one subfolder per month, named "
            "like 2026-09, by when each file was last modified. Use it when the user asks to "
            "sort or group a folder by date or by month. Nothing is replaced or deleted and at "
            "most 200 files move per run. To sort by file type instead use organize_folder; to "
            "move only old files out of Downloads use archive_old_downloads."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The folder to organise, e.g. '~/Pictures/Imports'."},
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, only report what would be moved and move nothing. Use it when the user asks what would happen or wants to check first.",
                },
            },
            "required": ["path"],
        },
    },
    "clean_desktop": {
        "fn": clean_desktop.run,
        "description": (
            "Clear the loose files off the Desktop by moving them into a folder on the Desktop "
            "called Desktop Cleanup, grouped by type. Use it when the user asks to clean up, "
            "tidy or clear their desktop. Folders on the Desktop stay where they are, nothing "
            "is deleted or replaced, and at most 200 files move per run. It only works on the "
            "Desktop; for any other folder use organize_folder. It does not close windows or "
            "quit apps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, only report what would be moved and move nothing. Use it when the user asks what would happen or wants to check first.",
                },
            },
            "required": [],
        },
    },
    "collect_screenshots": {
        "fn": collect_screenshots.run,
        "description": (
            "Move the screenshots and screen recordings that are lying on the Desktop into one "
            "Screenshots folder, by default the one in Pictures. Only files named like macOS "
            "screenshots move; nothing is deleted or replaced. Use it when the user wants "
            "their existing screenshots gathered up or put away. To take a new screenshot use "
            "screenshot_to_folder, and to clear everything off the Desktop use clean_desktop."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Folder to gather screenshots from. Defaults to the Desktop.",
                },
                "folder": {
                    "type": "string",
                    "description": "Folder to move them into. Defaults to '~/Pictures/Screenshots'.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, only report what would be moved and move nothing. Use it when the user asks what would happen or wants to check first.",
                },
            },
            "required": [],
        },
    },
    "archive_old_downloads": {
        "fn": archive_old_downloads.run,
        "description": (
            "Move files that have sat in the Downloads folder for a long time, 30 days unless "
            "told otherwise, into a folder inside Downloads called Old Downloads, grouped by "
            "month. They are moved out of the way, not deleted and not compressed. Use it when "
            "the user wants old downloads cleared out or put away. It only works on Downloads; "
            "to sort all of Downloads by type use organize_folder, and to zip something use "
            "archive_folder."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Move files not changed for at least this many days. Defaults to 30.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, only report what would be moved and move nothing. Use it when the user asks what would happen or wants to check first.",
                },
            },
            "required": [],
        },
    },
    "archive_folder": {
        "fn": archive_folder.run,
        "description": (
            "Zip a folder into a single zip file next to it, choosing a free name if one is "
            "taken, and leave the original folder untouched. Use it when the user asks to zip "
            "up, compress or archive a folder, for example to send it to someone. It makes a "
            "zip, not a second copy of the folder, which is backup_folder, and it includes "
            "everything in the folder; for a code project where dependencies should be left "
            "out use project_snapshot."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The folder to zip."},
                "destination_folder": {
                    "type": "string",
                    "description": "Folder to put the zip file in. Defaults to next to the original.",
                },
            },
            "required": ["path"],
        },
    },
    "project_snapshot": {
        "fn": project_snapshot.run,
        "description": (
            "Save a point-in-time zip of a code project into the Snapshots folder in the home "
            "folder, with a timestamp in its name and a line in that project's snapshot log, "
            "leaving out dependencies, build output and the git history. Use it when the user "
            "wants to snapshot or checkpoint a project before a risky change. It is not a git "
            "commit and does not touch the repository. For an ordinary folder, or a zip to "
            "send to someone, use archive_folder; for a plain second copy use backup_folder."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The project folder."},
                "note": {
                    "type": "string",
                    "description": "A few words on why the snapshot was taken, recorded in the log. Optional.",
                },
                "include_git": {"type": "boolean", "description": "Include the .git folder. Defaults to false."},
                "snapshots_dir": {
                    "type": "string",
                    "description": "Where snapshots are kept. Defaults to '~/Snapshots'.",
                },
            },
            "required": ["path"],
        },
    },
    "find_large_files": {
        "fn": find_large_files.run,
        "description": (
            "Report how much space a folder takes and name the largest files in it. Use it "
            "when the user asks what is taking up space in a folder or what the biggest files "
            "are; it looks in Downloads unless another folder is given. It only reports and "
            "removes nothing. For how full the whole disk is and which of the main folders is "
            "biggest, use storage_report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder to look in. Defaults to '~/Downloads'."},
                "min_size_mb": {
                    "type": "number",
                    "description": "Only count files at least this many megabytes. Defaults to 50.",
                },
                "limit": {"type": "integer", "description": "How many files to list. Defaults to 5."},
            },
            "required": [],
        },
    },
    "find_recent_files": {
        "fn": find_recent_files.run,
        "description": (
            "Find files that were downloaded, saved or changed in the last few days, newest "
            "first, looking in Downloads, the Desktop and Documents unless one folder is "
            "given. Use it when the user asks where a file they recently downloaded or saved "
            "went, or what they were working on lately. It only reports where the files are. "
            "If the user gives a name but no sense of when, use spotlight_search instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days back to look. Defaults to 3. Use 1 for today and 2 to include yesterday.",
                },
                "extension": {
                    "type": "string",
                    "description": "Only this file type, without the dot, e.g. 'pdf'. Optional.",
                },
                "name": {"type": "string", "description": "Part of the file name, if the user gave one. Optional."},
                "path": {
                    "type": "string",
                    "description": "Search only this folder instead of Downloads, Desktop and Documents. Optional.",
                },
                "limit": {"type": "integer", "description": "How many files to return. Defaults to 5."},
            },
            "required": [],
        },
    },
    "storage_report": {
        "fn": storage_report.run,
        "description": (
            "Report how much free space the disk has and how big each of the main home folders "
            "is: Desktop, Documents, Downloads, Movies, Music and Pictures. Use it when the "
            "user asks what is using up their disk or storage overall. It only reports. For "
            "what is big inside one particular folder use find_large_files, and for just the "
            "free space figure use get_disk_space."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "folder_summary": {
        "fn": folder_summary.run,
        "description": (
            "Give an overview of one folder: its total size, how many files of each kind and "
            "how many subfolders are directly inside, and the newest file. Use it when the "
            "user asks what is in a folder in general terms. It only reports. To get the "
            "actual list of names use list_files, and to look for a particular file use "
            "find_files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "The folder to describe, e.g. '~/Documents'."}},
            "required": ["path"],
        },
    },
    "daily_journal": {
        "fn": daily_journal.run,
        "description": (
            "Start today's journal, a Markdown file named for today's date in the Journal "
            "folder in Documents, if it does not exist yet, and optionally add a timestamped "
            "entry to it. Use it when the user says to start or open today's journal, or to "
            "add something to their journal or diary. An existing day's file is only ever "
            "added to, never replaced. It is a file, not the Notes app. For a one-line "
            "reminder with no journal involved use quick_note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The entry to add, in the user's words. Leave out to just start the day's file.",
                },
                "journal_dir": {
                    "type": "string",
                    "description": "Folder the journal is kept in. Defaults to '~/Documents/Journal'.",
                },
                "open": {
                    "type": "boolean",
                    "description": "Open the journal file on screen afterwards, when that is possible. Defaults to true.",
                },
            },
            "required": [],
        },
    },
    "screenshot_to_folder": {
        "fn": screenshot_to_folder.run,
        "description": (
            "Take a screenshot of the whole screen right now and save it into a Screenshots "
            "folder, by default the one in Pictures, instead of onto the Desktop. Use it when "
            "the user asks for a screenshot to be saved or filed somewhere, or simply to take "
            "a screenshot without cluttering the Desktop. It takes a new picture; to tidy "
            "away screenshots that already exist use collect_screenshots."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Folder to save into. Defaults to '~/Pictures/Screenshots'.",
                },
                "name": {
                    "type": "string",
                    "description": "A short label to start the file name with, e.g. 'login bug'. Optional.",
                },
            },
            "required": [],
        },
    },
    "duplicate_report": {
        "fn": duplicate_report.run,
        "description": (
            "Check a folder for files that are exact copies of each other and save the full "
            "list as a text report in Documents, then say how many sets there are and how much "
            "space they waste. Use it when the user asks whether a folder has duplicates or "
            "wants to find them; it checks Downloads unless another folder is given. It never "
            "removes or moves any copy. Not for making a copy of something, which is "
            "backup_folder."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder to check. Defaults to '~/Downloads'."},
                "save_report": {
                    "type": "boolean",
                    "description": "Write the list of duplicates to a report file. Defaults to true.",
                },
            },
            "required": [],
        },
    },
    "extract_latest_download": {
        "fn": extract_latest_download.run,
        "description": (
            "Unpack the most recently downloaded zip or tar archive in Downloads into a new "
            "folder next to it, keeping the archive. Use it when the user says to unzip or "
            "open the thing they just downloaded without naming it. If they name a specific "
            "archive or one outside Downloads, use extract_archive with its path instead. It "
            "does not open disk images or run installers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Folder to look for the newest archive in. Defaults to '~/Downloads'.",
                }
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
