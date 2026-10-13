"""Routing check: does a natural spoken request reach the right capability?

    cd agent
    ./.venv/bin/python scripts/skills_routing_check.py              # everything not cached yet
    ./.venv/bin/python scripts/skills_routing_check.py --report     # table from the cache, no calls
    ./.venv/bin/python scripts/skills_routing_check.py --only pomodoro,quick_timer
    ./.venv/bin/python scripts/skills_routing_check.py --payload    # size of the tool list

THIS CALLS THE REAL MODEL AND COSTS MONEY (about 280 requests for a cold
full run, each carrying the whole tool list). Nothing is ever dispatched:
each phrase is sent as one first turn through the real backend with the real
tool schemas and the real system prompt, built exactly as agent_loop.run()
builds them, and the tool calls the model asks for are recorded and thrown
away. No tool function runs, so the machine is not touched.

When the first turn only *looks* (file_info, get_clipboard, git_status...)
the look is answered with a canned result and one more turn is taken, so a
look-before-you-leap is scored on what it leaps to.

Every response is cached in scripts/skills_routing_cache.json, keyed by
phrase, run number and a hash of the whole prompt surface (system prompt +
every tool/skill schema). Editing any description changes the hash, so a
rerun re-asks; --stale-ok scores the newest cached answer whatever surface
it was asked against (marked with * in the table).

Exit status is non-zero when any phrase misses or hits a forbidden
capability on any run. An "unstable" phrase (runs disagree) is reported and
also fails. The model is not deterministic: a pass is evidence, not proof.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = AGENT_DIR.parent
sys.path.insert(0, str(AGENT_DIR))

CACHE_FILE = Path(__file__).resolve().parent / "skills_routing_cache.json"
RUNS = 2
WORKERS = 6

# Local state goes to a temp dir before the registries import: the tool list
# under test is the shipped one (no Library overrides, no generated skills
# from this machine), and nothing here may write into agent/.
TMP = Path(tempfile.mkdtemp(prefix="leutheria_routing_check_"))

from agent.core import logging_util  # noqa: E402

logging_util.LOG_DIR = TMP / "logs"
logging_util.LOG_FILE = logging_util.LOG_DIR / "events.jsonl"

from agent.core import preferences  # noqa: E402

preferences.PREFS_FILE = TMP / "preferences.json"
preferences._overrides.clear()

from agent.core import agent_loop  # noqa: E402
from agent.skills import registry as skills_registry  # noqa: E402
from agent.skills.registry import SKILLS, anthropic_skill_schemas  # noqa: E402
from agent.tools.registry import TOOLS, anthropic_tool_schemas  # noqa: E402

for _name in [n for n, s in SKILLS.items() if s.get("origin") == "generated"]:
    SKILLS.pop(_name)  # a learned skill on this machine is not part of the shipped surface
skills_registry.GENERATED_DIR = TMP / "generated"


def tool_schemas() -> list:
    return anthropic_tool_schemas() + anthropic_skill_schemas()


def system_prompt():
    return agent_loop._system_prompt()


# Read-only calls that are a look before acting, not a routing decision.
LOOK_FIRST = {
    "file_info", "list_files", "get_clipboard", "git_status", "get_current_datetime",
    "list_running_apps", "list_timers", "find_files", "get_volume", "describe_ui",
}

# What a look is told, so the second turn has something to act on.
CANNED = {
    "file_info": lambda a: {"ok": True, "exists": True, "kind": "folder", "path": a.get("path", ""),
                            "items": 14, "modified": "2026-10-12 18:02"},
    "list_files": lambda a: {"ok": True, "path": a.get("path", ""), "files": [
        "atlas", "taxes", "report.pdf", "report.md", "notes.txt", "photos.zip", "Screenshot 2026-10-12 at 09.14.03.png"]},
    "get_clipboard": lambda a: {"ok": True, "text": "https://example.com/long-read"},
    "git_status": lambda a: {"ok": True, "branch": "main", "modified": ["app.py"], "untracked": ["notes.md"],
                             "staged": [], "ahead": 1, "behind": 0, "remote_checked": False,
                             "upstream_note": "compared with the last fetch; the remote was not contacted",
                             "origin": "https://github.com/example/atlas.git"},
    "get_current_datetime": lambda a: {"ok": True, "datetime": "2026-10-13 09:30", "weekday": "Tuesday"},
    "list_running_apps": lambda a: {"ok": True, "apps": ["Finder", "Safari", "Music", "Notes"]},
    "list_timers": lambda a: {"ok": True, "count": 1, "timers": [
        {"id": "t1", "label": "pasta", "remaining_seconds": 240}]},
    "find_files": lambda a: {"ok": True, "count": 1, "files": [
        {"path": str(Path(a.get("path", "~/Downloads")) / "report.pdf"), "size_mb": 1.2,
         "modified": "2026-10-13 09:02"}]},
    "get_volume": lambda a: {"ok": True, "level": 60, "muted": False},
    "describe_ui": lambda a: {"ok": True, "app": a.get("app", "Notes"), "focused": "text area", "elements": []},
}


def case(phrase, expect, alt=(), forbid=(), why="", args=None, group=None):
    """expect: names that are the right route (the first is the one this case
    is filed under). alt: acceptable alternatives, each allowed for the stated
    `why`. forbid: similar-sounding capabilities that must not be called.
    "none" stands for answering in words with no tool call. args: optional
    predicate over the matched call's arguments."""
    return {"phrase": phrase, "expect": list(expect), "alt": list(alt), "forbid": list(forbid),
            "why": why, "args": args, "group": group or expect[0]}


def has(key, *needles):
    """Argument `key` contains every needle, case-insensitively."""
    return lambda a: all(n.lower() in str(a.get(key, "")).lower() for n in needles)


# --- skills: at least one phrasing each without the skill's name/keyword ------

SKILL_CASES = [
    case("start a new project called orbit", ["setup_project"]),
    case("I need a fresh repo for a thing called tidewater, get it set up", ["setup_project"],
         args=has("name", "tidewater")),
    case("remind me later that I need to renew my passport", ["quick_note"],
         alt=["none"], why="asking when 'later' is, to set a timed reminder, is a fair reading"),
    case("jot down somewhere that the plumber is coming thursday", ["quick_note"], args=has("text", "plumber")),
    case("back up my Documents/taxes folder before I mess with it", ["backup_folder"],
         forbid=["archive_folder", "project_snapshot"], args=has("path", "taxes")),
    case("make a copy of ~/Projects/atlas just in case", ["backup_folder"],
         alt=["copy_file"], why="a plain copy with a name the model picks is the same outcome",
         forbid=["archive_folder", "project_snapshot", "git_snapshot"]),
    case("give me my morning briefing", ["morning_briefing"]),
    case("morning, how's today looking", ["morning_briefing"]),
    case("how's my Mac doing", ["system_health_check"]),
    case("is everything okay with this computer", ["system_health_check"]),
    case("what's the weather like this week in Austin", ["weather_report"], args=has("place", "austin")),
    case("is it gonna rain this weekend in Seattle", ["weather_report"],
         alt=["get_weather"], why="get_weather with days set returns the same forecast"),
    case("set a timer for ten minutes", ["quick_timer"],
         alt=["set_timer"], why="same timer; only the on-screen banner differs", forbid=["pomodoro", "focus_mode"]),
    case("ten minutes on the clock, go", ["quick_timer"],
         alt=["set_timer"], why="same timer; only the on-screen banner differs", forbid=["pomodoro", "focus_mode"]),
    case("start a pomodoro", ["pomodoro"]),
    case("let's do twenty five minutes of work and then a five minute break", ["pomodoro"],
         forbid=["focus_mode"]),
    case("focus mode for half an hour", ["focus_mode"], args=lambda a: a.get("minutes") == 30),
    case("I need quiet to concentrate, shut the sound off for 45 minutes", ["focus_mode"],
         forbid=["pomodoro"], args=lambda a: a.get("minutes") == 45),
    case("I'm about to hop on a Zoom call, get me ready", ["meeting_prep"],
         args=lambda a: a.get("kind", "call") == "call"),
    case("heading into the conference room for an hour, the laptop's coming with me", ["meeting_prep"],
         args=lambda a: a.get("kind") == "in_person"),
    case("I'm done for the day", ["end_of_day"], forbid=["wind_down"]),
    case("wrapping up, anything I should know before I close the laptop", ["end_of_day"]),
    case("I'm winding down for the night", ["wind_down"], forbid=["end_of_day"]),
    case("getting ready for bed soon, make it quieter and nudge me in twenty minutes", ["wind_down"],
         args=lambda a: a.get("minutes") == 20),
    case("do I need to plug in", ["battery_check"],
         alt=["get_battery_status"], why="the percentage alone answers it; only the banner is lost"),
    case("will my battery be okay for a while", ["battery_check"],
         alt=["get_battery_status"], why="the percentage alone answers it; only the banner is lost"),
    case("organize my Downloads folder", ["organize_folder"], forbid=["archive_old_downloads", "organize_by_date"],
         args=has("path", "downloads")),
    case("my downloads is a mess, put everything into folders by what kind of file it is", ["organize_folder"],
         forbid=["archive_old_downloads", "clean_desktop"]),
    case("sort ~/Pictures/Imports by month", ["organize_by_date"], forbid=["organize_folder"]),
    case("group the files in ~/Pictures/Imports by when they're from", ["organize_by_date"],
         forbid=["organize_folder"]),
    case("clean up my desktop", ["clean_desktop"], forbid=["organize_folder"]),
    case("my desktop is covered in junk, deal with it", ["clean_desktop"],
         alt=["none"], why="asking what 'deal with it' may do to the files is cautious, not wrong",
         forbid=["trash_file"]),
    case("gather up all the screenshots on my desktop", ["collect_screenshots"],
         forbid=["screenshot_to_folder", "clean_desktop"]),
    case("put away those screen grabs lying around on the desktop", ["collect_screenshots"],
         forbid=["screenshot_to_folder", "clean_desktop"]),
    case("clear out my old downloads", ["archive_old_downloads"], forbid=["trash_file", "organize_folder"]),
    case("move the stuff that's been sitting in downloads for over two months out of the way",
         ["archive_old_downloads"], args=lambda a: 55 <= a.get("days", 0) <= 65),
    case("zip up my Documents/taxes folder", ["archive_folder"],
         alt=["zip_folder"], why="the underlying tool; same zip next to the folder",
         forbid=["backup_folder", "project_snapshot"]),
    case("compress ~/Documents/wedding so I can email it", ["archive_folder"],
         alt=["zip_folder"], why="the underlying tool; same zip next to the folder",
         forbid=["backup_folder", "project_snapshot"]),
    case("snapshot my project at ~/Projects/atlas before I refactor", ["project_snapshot"],
         forbid=["backup_folder", "archive_folder", "git_snapshot"]),
    case("checkpoint ~/Projects/atlas, I'm about to try something risky", ["project_snapshot"],
         forbid=["backup_folder", "archive_folder", "git_snapshot"]),
    case("what are the biggest files in my Downloads", ["find_large_files"],
         alt=["find_files"], why="find_files sorted by size is the same list without the folder total",
         forbid=["storage_report"]),
    case("what's hogging space in my Movies folder", ["find_large_files"],
         alt=["folder_size"], why="folder_size answers 'what is taking space in a folder' item by item",
         forbid=["storage_report"], args=has("path", "movies")),
    case("where did that PDF I just downloaded go", ["find_recent_files"],
         alt=["find_files"], why="find_files on Downloads, newest first, finds the same file",
         forbid=["spotlight_search"]),
    case("what was I working on yesterday", ["find_recent_files"]),
    case("what's using up all my storage", ["storage_report"], forbid=["find_large_files"]),
    case("why is my disk so full", ["storage_report"],
         alt=["get_disk_space"], why="a first look at the free-space figure before the breakdown"),
    case("what's in my Documents folder, roughly", ["folder_summary"], forbid=["spotlight_search"]),
    case("give me the gist of what's sitting in ~/Projects", ["folder_summary"],
         alt=["list_files"], why="for a folder of projects the names are the gist"),
    case("add to my journal: had a good run this morning", ["daily_journal"],
         forbid=["quick_note"], args=has("text", "run")),
    case("start today's diary page", ["daily_journal"], forbid=["quick_note", "write_new_file"]),
    case("take a screenshot", ["screenshot_to_folder"],
         alt=["take_screenshot"], why="the plain tool; lands on the Desktop instead of Pictures",
         forbid=["collect_screenshots"]),
    case("grab a picture of my screen and file it away", ["screenshot_to_folder"],
         forbid=["collect_screenshots", "take_screenshot"]),
    case("do I have any duplicate files in Downloads", ["duplicate_report"],
         alt=["find_duplicates"], why="same finding, without the written report"),
    case("find the files in my Pictures folder that are the same thing twice", ["duplicate_report"],
         alt=["find_duplicates"], why="same finding, without the written report", args=has("path", "pictures")),
    case("unzip the thing I just downloaded", ["extract_latest_download"]),
    case("unpack that archive I just grabbed off the web", ["extract_latest_download"]),
    case("make me a python project called atlas", ["new_python_project"], forbid=["setup_project"]),
    case("I want to write a little tool in python, call it scraper, set up the files", ["new_python_project"],
         forbid=["setup_project", "code_scratchpad"], args=has("name", "scraper")),
    case("new node project called atlas-api", ["new_node_project"], forbid=["setup_project"]),
    case("scaffold a javascript app named chatbot", ["new_node_project"],
         forbid=["setup_project", "new_web_page"]),
    case("make a landing page called portfolio", ["new_web_page"], forbid=["new_node_project"]),
    case("I need an html and css starter named birthday-invite", ["new_web_page"],
         forbid=["code_scratchpad", "new_node_project"]),
    case("commit everything in ~/Projects/atlas with the message fix login", ["git_snapshot"],
         forbid=["project_snapshot", "run_command"], args=has("message", "fix login")),
    case("save my changes in the atlas repo at ~/Projects/atlas, message is wip", ["git_snapshot"],
         forbid=["project_snapshot", "backup_folder", "run_command"]),
    case("pull the latest in ~/Projects/atlas", ["git_sync"], forbid=["run_command"],
         args=lambda a: a.get("action") == "pull"),
    case("send my commits in ~/Projects/atlas up to the remote", ["git_sync"], forbid=["run_command"],
         args=lambda a: a.get("action") == "push"),
    case("clone anthropics/anthropic-sdk-python", ["clone_repo"], forbid=["github_open", "run_command"]),
    case("download the pallets flask repo from github onto my machine", ["clone_repo"],
         forbid=["github_open", "run_command"], args=has("repo", "pallets/flask")),
    case("what is the repo at ~/Projects/atlas", ["repo_overview"]),
    case("where do things stand with ~/Projects/atlas", ["repo_overview"],
         alt=["git_status"], why="git_status alone answers 'where do things stand' for a repository"),
    case("open my atlas project, it's in ~/Projects/atlas", ["open_workspace"],
         alt=["open_with"], why="open_with on the folder with a code editor is the same window",
         forbid=["setup_project"]),
    case("let's get to work on ~/Projects/atlas", ["open_workspace"], forbid=["setup_project"]),
    case("search for best ramen in Austin", ["web_search"], forbid=["research_topic"]),
    case("google mechanical keyboards", ["web_search"], forbid=["research_topic"]),
    case("research heat pumps for me", ["research_topic"], forbid=["web_search"]),
    case("I want to read up on the Byzantine empire", ["research_topic"], forbid=["web_search"]),
    case("look up ephemeral in the dictionary", ["define_term"], forbid=["web_search"]),
    case("show me the definition of sanguine", ["define_term"], forbid=["web_search"]),
    case("open the issues for facebook/react on github", ["github_open"],
         forbid=["clone_repo"], args=lambda a: a.get("section") == "issues"),
    case("show me the pull requests on torvalds linux", ["github_open"],
         forbid=["clone_repo"], args=lambda a: a.get("section") == "pulls"),
    case("save what I copied to my notes file", ["clipboard_to_note"], forbid=["clipboard_to_file", "quick_note"]),
    case("stash what I just copied somewhere so I don't lose it", ["clipboard_to_note"],
         alt=["clipboard_to_file"], why="'somewhere' does not say running file or its own file"),
    case("save my clipboard as a file called quote.txt in Documents", ["clipboard_to_file"],
         forbid=["clipboard_to_note", "write_new_file"], args=has("path", "quote.txt")),
    case("put what I copied into its own file", ["clipboard_to_file"], forbid=["clipboard_to_note"]),
    case("save the content of https://example.com/article so I have it", ["save_page_notes"],
         forbid=["read_later"]),
    case("keep a copy of the text of https://en.wikipedia.org/wiki/Entropy", ["save_page_notes"],
         forbid=["read_later"]),
    case("add https://example.com/long-read to my reading list", ["read_later"], forbid=["save_page_notes"]),
    case("I copied a link, hang on to it for me to read later", ["read_later"],
         forbid=["save_page_notes", "clipboard_to_note"]),
    case("give me a scratch python file to try something", ["code_scratchpad"], forbid=["new_python_project"]),
    case("I want somewhere quick to mess around with some typescript", ["code_scratchpad"],
         forbid=["new_node_project"], args=lambda a: a.get("language") == "typescript"),
]

# --- tools a person would ask for directly ------------------------------------

TOOL_CASES = [
    case("open Calculator", ["open_app"]),
    case("turn it down a bit", ["set_volume"], args=lambda a: a.get("change", 0) < 0),
    case("how loud is it right now", ["get_volume"]),
    case("pause the music", ["media_control"], forbid=["set_volume"], args=lambda a: a.get("action") == "play_pause"),
    case("skip this song", ["media_control"], args=lambda a: a.get("action") == "next"),
    case("what's the weather in Paris right now", ["get_weather"], forbid=["weather_report", "morning_briefing"]),
    case("I live in Denver, remember that", ["set_home_location"], args=has("place", "denver")),
    case("remind me in 20 minutes to take the pizza out", ["set_timer"],
         forbid=["quick_note", "quick_timer"], args=lambda a: a.get("minutes") == 20 and bool(a.get("message"))),
    case("how long is left on my timer", ["list_timers"]),
    case("cancel the pasta timer", ["cancel_timer"]),
    case("open ~/Documents/resume.pdf", ["open_file"], forbid=["open_with", "read_file"]),
    case("show me where ~/Documents/resume.pdf is", ["reveal_in_finder"], forbid=["open_file"]),
    case("what's my battery at", ["get_battery_status"], forbid=["battery_check", "system_health_check"]),
    case("how much free space do I have", ["get_disk_space"], forbid=["storage_report"]),
    case("what time is it", ["get_current_datetime"]),
    case("what apps do I have open", ["list_running_apps"]),
    case("am I online", ["get_network_status"], forbid=["system_health_check"]),
    case("how much memory is free", ["get_system_info"], forbid=["system_health_check"]),
    case("find the invoice pdfs in my Downloads", ["find_files"], forbid=["spotlight_search"],
         args=lambda a: "invoice" in str(a.get("name", "")).lower()),
    case("which of my notes in ~/Notes mentions the wifi password", ["search_file_contents"],
         forbid=["spotlight_search", "web_search"]),
    case("unzip ~/Downloads/photos.zip", ["extract_archive"], forbid=["extract_latest_download"]),
    case("trash ~/Desktop/old draft.txt", ["trash_file"], forbid=["run_command"]),
    case("open youtube", ["open_url"], alt=["web_search"], why="web_search with engine youtube opens YouTube too"),
    case("summarize https://example.com/article for me", ["fetch_url"], forbid=["open_url", "save_page_notes"]),
    case("what branch am I on in ~/Projects/atlas", ["git_status"], forbid=["run_command"]),
    case("list what's on my Desktop", ["list_files"]),
    case("what's on my clipboard", ["get_clipboard"]),
    case("rename ~/Desktop/a.txt to b.txt", ["move_file"], forbid=["run_command"]),
    case("write a file called plan.md in Documents with a three line plan for the week", ["write_new_file"],
         forbid=["quick_note", "daily_journal", "append_text"]),
    case("how big is ~/Movies/trip.mov", ["file_info"], forbid=["find_large_files"]),
]

# --- negatives: a similar-sounding capability must NOT be chosen ---------------

NEGATIVE_CASES = [
    case("create a note called groceries", ["focus_app", "open_app"],
         forbid=["quick_note", "write_new_file", "daily_journal", "clipboard_to_note", "append_text"],
         group="NEG notes app, not a file (BUGS #16)"),
    case("make a note in TextEdit that says call mom", ["focus_app", "open_app"],
         forbid=["quick_note", "write_new_file", "daily_journal", "append_text"],
         group="NEG named app, not a scratch log"),
    case("back up my project at ~/Projects/atlas", ["backup_folder"],
         forbid=["archive_folder", "project_snapshot", "git_snapshot", "zip_folder"],
         group="NEG back up vs zip vs snapshot"),
    case("zip my project at ~/Projects/atlas", ["archive_folder"],
         alt=["zip_folder", "project_snapshot"],
         why="zip_folder is the same zip; project_snapshot is where archive_folder's own description sends code projects",
         forbid=["backup_folder", "git_snapshot"], group="NEG zip vs back up vs snapshot"),
    case("find my resume", ["spotlight_search"], forbid=["find_recent_files", "list_files", "folder_summary"],
         group="NEG find by name vs recent vs listing"),
    case("what's in Documents", ["folder_summary", "list_files"],
         forbid=["spotlight_search", "find_recent_files", "storage_report"],
         group="NEG listing vs search"),
    case("open ~/Documents/report.md in VS Code", ["open_with"], forbid=["open_workspace", "open_file"],
         args=lambda a: "code" in str(a.get("app", "")).lower(), group="NEG one file in an editor vs a workspace"),
    case("set a timer for 25 minutes", ["quick_timer", "set_timer"], forbid=["pomodoro", "focus_mode"],
         group="NEG timer vs pomodoro"),
    case("what does ephemeral mean", ["none"], forbid=["define_term", "web_search", "research_topic"],
         group="NEG answer vs open a dictionary page"),
    case("search for the word invoice in my ~/Notes folder", ["search_file_contents"],
         forbid=["web_search", "spotlight_search"], group="NEG search in files vs web search"),
    case("look up the population of Iceland and tell me", ["none", "fetch_url"],
         forbid=["web_search", "research_topic", "define_term"], group="NEG tell me vs open tabs"),
    case("what's the weather today in Chicago", ["get_weather"], forbid=["weather_report", "morning_briefing"],
         group="NEG today vs outlook vs briefing"),
    case("what's on my schedule today", ["none"], forbid=["morning_briefing", "end_of_day"],
         group="NEG calendar is not the briefing"),
    case("mute my Mac", ["set_volume"], forbid=["focus_mode", "meeting_prep", "wind_down"],
         args=lambda a: a.get("muted") is True, group="NEG mute vs focus mode"),
    case("take a screenshot and put it on my desktop", ["take_screenshot", "screenshot_to_folder"],
         forbid=["collect_screenshots"], group="NEG take vs collect screenshots"),
    case("make a new folder called atlas in ~/Projects", ["create_folder"],
         forbid=["setup_project", "new_python_project", "new_node_project"], group="NEG folder vs project"),
    case("copy ~/Documents/taxes to /Volumes/Backup", ["copy_file"], forbid=["backup_folder", "archive_folder"],
         group="NEG copy somewhere vs back up alongside"),
    case("how much space is my Downloads folder taking up", ["folder_size", "find_large_files"],
         alt=["file_info", "folder_summary"], why="both report something about the folder; only folder_summary has the total",
         forbid=["storage_report", "get_disk_space"], group="NEG one folder vs whole disk"),
]

GROUPS = [("skills", SKILL_CASES), ("tools", TOOL_CASES), ("negatives", NEGATIVE_CASES)]


# --- cache --------------------------------------------------------------------

_cache_lock = threading.Lock()


def surface_hash() -> str:
    blob = json.dumps({"system": system_prompt(), "tools": tool_schemas()}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {"entries": []}


def save_cache(cache: dict) -> None:
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    os.replace(tmp, CACHE_FILE)


def cached(cache: dict, phrase: str, run: int, surface: str, stale_ok: bool):
    matches = [e for e in cache["entries"] if e["phrase"] == phrase and e["run"] == run]
    exact = [e for e in matches if e["surface"] == surface]
    if exact:
        return exact[-1]
    if stale_ok and matches:
        return matches[-1]
    return None


# --- one phrase, one run ------------------------------------------------------


def ask(backend, phrase: str) -> dict:
    """One first turn, plus one more if that turn only looked. Returns the
    calls of each turn and the text; no tool is executed."""
    tools, system = tool_schemas(), system_prompt()
    messages = [{"role": "user", "content": phrase}]
    turns = []
    for _ in range(2):
        turn = backend.generate(messages, tools, system)
        calls = [{"name": c.name, "args": c.args} for c in turn.tool_calls]
        turns.append({"calls": calls, "text": turn.text})
        if not calls or not all(c["name"] in LOOK_FIRST for c in calls):
            break
        messages.append({"role": "assistant", "content": turn.content})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(CANNED[c.name](c.args))}
            for c in turn.tool_calls
        ]})
    return {"turns": turns}


def score(c: dict, entry: dict) -> dict:
    """hit / alt / miss / forbidden for one run, and what was called."""
    turns = entry["turns"]
    calls = [call for t in turns for call in t["calls"]]
    names = [call["name"] for call in calls] or ["none"]
    looked = len(turns) > 1
    forbidden = [n for n in names if n in c["forbid"]]
    # A look that is itself the expected route (list_files for "what's on my
    # Desktop") counts; otherwise looks are transparent.
    acted = [n for n in names if n not in LOOK_FIRST or n in c["expect"] or n in c["alt"]] or ["none"]
    verdict, matched = "miss", None
    for pool, label in ((c["expect"], "hit"), (c["alt"], "alt")):
        found = next((n for n in acted if n in pool), None)
        if found:
            verdict, matched = label, found
            break
    if forbidden:
        verdict = "forbidden"
    bad_args = False
    if verdict == "hit" and c["args"] and matched != "none":
        call_args = next(call["args"] for call in calls if call["name"] == matched)
        try:
            bad_args = not c["args"](call_args)
        except Exception:
            bad_args = True
        if bad_args:
            verdict = "args"
    return {"verdict": verdict, "names": names, "looked": looked, "calls": calls,
            "text": (turns[-1]["text"] or "")[:160]}


def selected(only):
    for group, cases in GROUPS:
        for c in cases:
            if only and not (only & ({c["group"]} | set(c["expect"]) | set(c["alt"]) | set(c["forbid"]) | {group})):
                continue
            yield group, c


def load_key() -> None:
    """The key is in the repo-root .env, not agent/ -- passed by explicit path
    because this script's cwd is not the repo root. A git worktree has no
    .env of its own, so the enclosing checkout's is used."""
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        for parent in REPO_ROOT.parents:
            if (parent / ".env").exists() and (parent / "agent").is_dir():
                load_dotenv(parent / ".env")
                break


def run_missing(cache, surface, only, stale_ok, limit):
    load_key()
    jobs = [(c["phrase"], run) for _, c in selected(only) for run in range(RUNS)
            if cached(cache, c["phrase"], run, surface, stale_ok) is None]
    if limit is not None:
        jobs = jobs[:limit]
    if not jobs:
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set (expected in the repo-root .env); use --report for cached results")
        sys.exit(2)
    from agent.core.llm_anthropic import AnthropicBackend

    backend = AnthropicBackend()
    print(f"asking the model: {len(jobs)} phrase-runs (surface {surface})", flush=True)
    model_calls = [0]

    def work(job):
        phrase, run = job
        try:
            entry = ask(backend, phrase)
        except Exception as e:  # one failed request must not lose the others
            print(f"  error on {phrase!r}: {type(e).__name__}: {e}", flush=True)
            return
        with _cache_lock:
            model_calls[0] += len(entry["turns"])
            cache["entries"].append({"phrase": phrase, "run": run, "surface": surface, **entry})
            save_cache(cache)

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(work, jobs))
    print(f"model calls made: {model_calls[0]}", flush=True)
    return model_calls[0]


def report(cache, surface, only, stale_ok) -> bool:
    ok = True
    rows, confusions, per_target = [], [], {}
    for group, c in selected(only):
        runs = []
        for run in range(RUNS):
            entry = cached(cache, c["phrase"], run, surface, stale_ok)
            if entry is None:
                runs.append(None)
                continue
            s = score(c, entry)
            s["stale"] = entry["surface"] != surface
            runs.append(s)
        done = [r for r in runs if r]
        if not done:
            status = "not run"
        else:
            verdicts = {r["verdict"] for r in done}
            routes = {tuple(n for n in r["names"] if n not in LOOK_FIRST or n in c["expect"]) for r in done}
            if verdicts <= {"hit"}:
                status = "hit"
            elif verdicts <= {"hit", "alt"}:
                status = "alt" if verdicts == {"alt"} else "hit/alt"
            elif verdicts & {"hit", "alt"}:
                status = "UNSTABLE"
            else:
                status = "MISS"
            if len(routes) > 1 and status in ("hit", "alt", "hit/alt"):
                status += " (varies)"
            if status in ("UNSTABLE", "MISS"):
                ok = False
        rows.append((group, c, status, runs))
        per_target.setdefault((group, c["group"]), []).append(status)
        for r in done:
            if r["verdict"] in ("miss", "forbidden", "args"):
                confusions.append((c, r))

    for group_name in ("skills", "tools", "negatives"):
        group_rows = [r for r in rows if r[0] == group_name]
        if not group_rows:
            continue
        print(f"\n== {group_name} ==")
        for _, c, status, runs in group_rows:
            shown = []
            for r in runs:
                if r is None:
                    shown.append("-")
                    continue
                mark = "*" if r["stale"] else ""
                look = "look>" if r["looked"] else ""
                shown.append(f"{r['verdict']}{mark}:{look}{'+'.join(r['names'])}")
            print(f"{status:<16} {c['group'][:34]:<34} {c['phrase'][:62]!r:<64} {' | '.join(shown)}")

    print("\n== per capability ==")
    for (group_name, target), statuses in per_target.items():
        if any(s in ("MISS",) for s in statuses):
            overall = "MISS"
        elif any(s == "UNSTABLE" for s in statuses):
            overall = "UNSTABLE"
        elif any(s == "not run" for s in statuses):
            overall = "not run"
        elif all(s.startswith("hit") and "alt" not in s for s in statuses):
            overall = "hit"
        else:
            overall = "hit (alt allowed)"
        print(f"{overall:<18} {target}  [{', '.join(statuses)}]")

    if confusions:
        print("\n== confusions ==")
        for c, r in confusions:
            want = "/".join(c["expect"])
            detail = "; ".join(f"{call['name']}({json.dumps(call['args'], ensure_ascii=False)[:110]})" for call in r["calls"])
            print(f"{r['verdict']:<9} {c['phrase']!r}\n          wanted {want}; got {detail or 'no tool: ' + r['text']!r}")
    allowed = [(c, r) for _, c, _, runs in rows for r in runs if r and r["verdict"] == "alt"]
    if allowed:
        print("\n== alternatives allowed ==")
        seen = set()
        for c, r in allowed:
            key = (c["phrase"], tuple(r["names"]))
            if key in seen:
                continue
            seen.add(key)
            print(f"{c['phrase']!r}: {'+'.join(r['names'])} instead of {c['expect'][0]} -- {c['why']}")
    total = len(rows)
    counts = {}
    for _, _, status, _ in rows:
        counts[status.split(" ")[0]] = counts.get(status.split(" ")[0], 0) + 1
    print(f"\n{total} phrases x {RUNS} runs: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    return ok


def payload():
    tools, system = tool_schemas(), system_prompt()
    blob = json.dumps(tools)
    print(f"capabilities sent per request: {len(tools)} ({len(TOOLS)} tools + {len(SKILLS)} skills)")
    print(f"tools JSON: {len(blob):,} characters; system prompt: {len(system or ''):,} characters")
    try:
        load_key()
        import anthropic

        from agent.core.llm_anthropic import MODEL

        client = anthropic.Anthropic()
        message = [{"role": "user", "content": "hi"}]
        with_tools = client.messages.count_tokens(model=MODEL, messages=message, tools=tools, system=system).input_tokens
        no_tools = client.messages.count_tokens(model=MODEL, messages=message, system=system).input_tokens
        bare = client.messages.count_tokens(model=MODEL, messages=message).input_tokens
        print(f"input tokens for a one-word request: {with_tools:,} "
              f"(tools {with_tools - no_tools:,}, system prompt {no_tools - bare:,}, message {bare})")
    except Exception as e:
        print(f"token count unavailable ({type(e).__name__}: {e}); roughly {len(blob) // 4:,} tokens at 4 chars/token")
    longest = sorted(tools, key=lambda t: -len(json.dumps(t)))[:10]
    print("longest entries (description + schema, characters):")
    for t in longest:
        print(f"  {len(json.dumps(t)):>5}  {t['name']}  (description {len(t['description'])})")
    longest_desc = sorted(tools, key=lambda t: -len(t["description"]))[:10]
    print("longest descriptions (characters):")
    for t in longest_desc:
        print(f"  {len(t['description']):>5}  {t['name']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--report", action="store_true", help="score the cache; make no model calls")
    parser.add_argument("--stale-ok", action="store_true", help="accept answers cached against an older prompt surface")
    parser.add_argument("--only", default="", help="comma-separated capability names or groups to restrict to")
    parser.add_argument("--limit", type=int, default=None, help="at most this many phrase-runs this time")
    parser.add_argument("--payload", action="store_true", help="print the size of the tool list and exit")
    parser.add_argument("--list", action="store_true", help="print the phrases and exit")
    args = parser.parse_args()
    if args.payload:
        payload()
        return
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    if args.list:
        for group, c in selected(only):
            print(f"{group:<10} {'/'.join(c['expect']):<28} {c['phrase']}")
        return
    missing_skills = set(n for n, s in SKILLS.items()) - {c["group"] for c in SKILL_CASES}
    if missing_skills and not only:
        print(f"skills with no phrase: {sorted(missing_skills)}")
        sys.exit(1)
    cache = load_cache()
    surface = surface_hash()
    if not args.report:
        run_missing(cache, surface, only, args.stale_ok, args.limit)
    ok = report(cache, surface, only, args.stale_ok or args.report)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
