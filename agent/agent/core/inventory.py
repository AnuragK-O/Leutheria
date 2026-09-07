import json

from agent.core import preferences, trust
from agent.core.logging_util import LOG_FILE
from agent.skills.registry import SKILLS
from agent.tools.registry import TOOLS

# Lifecycle events worth showing in the UI's Activity view, mapped to the
# label it renders. Everything else in the event log is either per-message
# noise or already visible in the chat transcript.
HISTORY_EVENTS = {
    "skill_proposed": "proposed",
    "skill_registered": "saved",
    "skill_declined": "declined",
}

HISTORY_LIMIT = 200  # newest N, so a long-lived log doesn't bloat the payload


def _scan_log() -> tuple:
    """One pass over the event log producing (usage, history).

    usage: name -> {"runs", "failures", "last_used"} for every tool/skill that
    has ever actually been dispatched. history: recent skill-lifecycle events,
    newest first.
    """
    usage: dict = {}
    history: list = []

    if not LOG_FILE.exists():
        return usage, history

    with LOG_FILE.open() as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            event = record.get("type")

            if event == "tool_call":
                name = record.get("tool")
                if not name:
                    continue
                stats = usage.setdefault(name, {"runs": 0, "failures": 0, "last_used": None})
                stats["runs"] += 1
                result = record.get("result")
                if isinstance(result, dict) and result.get("ok") is False:
                    stats["failures"] += 1
                stats["last_used"] = record.get("ts")

            elif event in HISTORY_EVENTS:
                history.append(
                    {
                        "ts": record.get("ts"),
                        "event": HISTORY_EVENTS[event],
                        "name": record.get("name"),
                        "confident": record.get("confident"),
                        "uncertain_count": record.get("uncertain_count"),
                    }
                )

    history.reverse()
    return usage, history[:HISTORY_LIMIT]


def _entry(name: str, description: str, input_schema: dict, kind: str, default_confirm: bool, usage: dict, extra: dict = None) -> dict:
    """Shared shape for a tool or skill row. `default_confirm` is the built-in
    policy; `requires_confirmation` is what actually applies after any user
    override, and `is_overridden` is what lets the UI show a "modified" marker
    and offer a reset."""
    stats = usage.get(name, {"runs": 0, "failures": 0, "last_used": None})
    enabled = preferences.is_enabled(name)
    confirms = preferences.requires_confirmation(name, default_confirm)
    # "Overridden" means the behaviour actually differs from the default, not
    # merely that a preferences entry exists -- an override that happens to
    # restate the default shouldn't show up as a customization to undo.
    return {
        "name": name,
        "kind": kind,
        "description": description,
        "input_schema": input_schema,
        "enabled": enabled,
        "requires_confirmation": confirms,
        "default_requires_confirmation": default_confirm,
        "is_overridden": (not enabled) or confirms != default_confirm,
        "runs": stats["runs"],
        "failures": stats["failures"],
        "last_used": stats["last_used"],
        **(extra or {}),
    }


def snapshot() -> dict:
    """Everything the desktop UI's Library and Activity views render, in one
    message -- the UI re-requests this after any change rather than trying to
    keep a local mirror of agent state in sync."""
    usage, history = _scan_log()

    tools = [
        _entry(
            name,
            tool["description"],
            tool["input_schema"],
            "tool",
            tool["safety"] == "destructive",
            usage,
            {"safety": tool["safety"]},
        )
        for name, tool in TOOLS.items()
    ]

    skills = [
        _entry(
            name,
            skill["description"],
            skill["input_schema"],
            "skill",
            False,  # skills confirm per-step by default, not up front
            usage,
            {
                "origin": skill.get("origin", "built-in"),
                "steps": skill.get("steps", []),
                "source_signature": skill.get("source_signature"),
                "created_at": skill.get("created_at"),
                "deletable": skill.get("origin") == "generated",
            },
        )
        for name, skill in SKILLS.items()
    ]

    tools.sort(key=lambda entry: entry["name"])
    skills.sort(key=lambda entry: entry["name"])

    return {
        "tools": tools,
        "skills": skills,
        "history": history,
        "trusted": trust.list_trusted(),
    }
