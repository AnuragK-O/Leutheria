"""Check of the generated-skill format: metadata, validation, load/migrate,
and the template interpreter's placeholder handling.

    cd agent
    ./.venv/bin/python scripts/skill_format_check.py

Everything runs against a temp generated/ dir, a temp event log and a temp
preferences file -- nothing is written to the real ones. Exits non-zero on
the first failed check.
"""

import asyncio
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

TMP = Path(tempfile.mkdtemp(prefix="leutheria_skill_check_"))

# Redirect the event log before anything imports the registry (which loads
# and may log at import time).
from agent.core import logging_util  # noqa: E402

logging_util.LOG_DIR = TMP / "logs"
logging_util.LOG_FILE = logging_util.LOG_DIR / "events.jsonl"

from agent.core import preferences  # noqa: E402

preferences.PREFS_FILE = TMP / "preferences.json"

from agent.core import skill_learning  # noqa: E402
from agent.skills import registry, template_skill, validate  # noqa: E402

GEN = TMP / "generated"
registry.GENERATED_DIR = GEN

passed = 0


def check(condition, label):
    global passed
    if not condition:
        print(f"FAIL  {label}")
        sys.exit(1)
    passed += 1
    print(f"ok    {label}")


def events(kind):
    if not logging_util.LOG_FILE.exists():
        return []
    records = [json.loads(line) for line in logging_util.LOG_FILE.read_text().splitlines()]
    return [r for r in records if r["type"] == kind]


def base_definition(name="make_and_list"):
    return {
        "name": name,
        "description": "Create a folder and list what's inside it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Folder name."},
                "base": {"type": "string", "description": "Parent directory."},
            },
            "required": ["folder"],
        },
        "steps": [
            {"tool": "create_folder", "args": {"path": "{base}/{folder}"}},
            {"tool": "list_files", "args": {"path": "{base}/{folder}"}},
        ],
        "source_signature": ["create_folder", "list_files"],
    }


def rejected(definition):
    result = registry.register_skill(definition)
    return (not result["ok"]), result.get("error", "")


print(f"temp dir: {TMP}\n")

# --- 1. a valid learned definition registers and gets metadata -------------
proposal = {**base_definition(), "uncertain_params": [
    {"name": "base", "guessed_value": str(TMP / "work"), "question": "Always there?"}]}
final = skill_learning.finalize_definition(proposal, {"base": False})
result = registry.register_skill(final)
check(result["ok"], f"valid definition registers ({result})")
path = GEN / "make_and_list.json"
on_disk = json.loads(path.read_text())
check(set(validate.METADATA_KEYS) <= set(on_disk), "file has id/content_hash/version/source/requires/created_at")
check(on_disk["version"] == 1 and on_disk["source"] == "learned", "version 1, source learned")
check(on_disk["requires"] == ["create_folder", "list_files"], "requires is the sorted tool list")
check(on_disk["content_hash"] == validate.content_hash(on_disk), "stored hash matches content")
check("base" not in on_disk["input_schema"]["properties"], "'always this value' param baked out of schema")
check(registry.SKILLS["make_and_list"]["id"] == on_disk["id"], "SKILLS entry carries the metadata")
again = registry.register_skill(final)
check(again["ok"] and again.get("unchanged"), "re-registering identical content is a no-op")

# --- 2. hash stability ------------------------------------------------------
d = base_definition()
reordered = {
    "steps": [{"args": s["args"], "tool": s["tool"]} for s in d["steps"]],
    "input_schema": {"required": ["folder"], "properties": dict(reversed(list(d["input_schema"]["properties"].items()))),
                     "type": "object"},
    "name": "other_name", "description": "Different words entirely.",
}
check(validate.content_hash(d) == validate.content_hash(reordered),
      "hash stable across key order, name and description")
prose = copy.deepcopy(d)
prose["input_schema"]["properties"]["folder"]["description"] = "reworded"
check(validate.content_hash(d) == validate.content_hash(prose), "param descriptions are outside the hash")
changed = copy.deepcopy(d)
changed["steps"][1]["args"]["path"] = "{base}"
check(validate.content_hash(d) != validate.content_hash(changed), "hash changes when a step changes")
swapped = copy.deepcopy(d)
swapped["steps"].reverse()
check(validate.content_hash(d) != validate.content_hash(swapped), "hash changes when step order changes")
typed = copy.deepcopy(d)
typed["input_schema"]["required"] = ["base", "folder"]
check(validate.content_hash(d) != validate.content_hash(typed), "hash changes when required params change")

# --- 3. names ---------------------------------------------------------------
for bad in ["../x", "a/b", "CON", "con", "ѕkill", "skill\n", "a" * 65, "", "1abc", "run_command",
            "setup_project", "x.json", None]:
    definition = {**base_definition(), "name": bad}
    ok, error = rejected(definition)
    check(ok, f"name {bad!r} rejected ({error})")
check(not (TMP / "x.json").exists() and not list(TMP.glob("*.json")), "nothing written outside generated/")
try:
    validate.skill_path(GEN, "../escape")
    check(False, "skill_path refuses traversal")
except validate.SkillValidationError:
    check(True, "skill_path refuses traversal")

# --- 4. content rules -------------------------------------------------------
def variant(mutate, name):
    definition = base_definition(name)
    mutate(definition)
    return definition


cases = {
    "unknown tool": lambda d: d["steps"][0].update(tool="format_disk"),
    "skill as a step": lambda d: d["steps"].append({"tool": "setup_project", "args": {"name": "{folder}"}}),
    "generated skill as a step": lambda d: d["steps"].append({"tool": "make_and_list", "args": {"folder": "{folder}"}}),
    "undeclared placeholder": lambda d: d["steps"][0]["args"].update(path="{base}/{folder}/{sub}"),
    "non-JSON arg (set)": lambda d: d["steps"][0]["args"].update(path={1, 2}),
    "non-JSON arg (NaN)": lambda d: d["steps"][0]["args"].update(path=float("nan")),
    "oversized description": lambda d: d.update(description="x" * 1001),
    "empty description": lambda d: d.update(description="   "),
    "empty steps": lambda d: d.update(steps=[]),
    "unused required param": lambda d: d["input_schema"]["properties"].update(extra={"type": "string"})
    or d["input_schema"]["required"].append("extra"),
    "unused optional param": lambda d: d["input_schema"]["properties"].update(extra={"type": "string"}),
    "arg the tool doesn't take": lambda d: d["steps"][0]["args"].update(mode="0777"),
    "missing required tool arg": lambda d: d["steps"][1].update(args={}),
    "extra step key": lambda d: d["steps"][0].update(note="hi"),
    "unknown top-level key": lambda d: d.update(code="import os"),
    "param type object": lambda d: d["input_schema"]["properties"]["folder"].update(type="object"),
    "required not declared": lambda d: d["input_schema"]["required"].append("ghost"),
}
for label, mutate in cases.items():
    ok, error = rejected(variant(mutate, "case_" + label.split()[0].replace("-", "_").lower()))
    check(ok, f"{label} rejected ({error})")

d = base_definition("brace_ok")
d["steps"][0]["args"]["path"] = "{base}/{folder}/{{literal}}"
check(registry.register_skill(d)["ok"], "escaped {{literal}} is not a placeholder")
registry.delete_skill("brace_ok")
check(not (GEN / "brace_ok.json").exists(), "delete_skill removes the file")

# finalize prunes an unused optional param; it doesn't paper over a required one.
d = base_definition("pruned")
d["input_schema"]["properties"]["unused"] = {"type": "string"}
check("unused" not in skill_learning.finalize_definition(d, {})["input_schema"]["properties"],
      "finalize prunes an unused optional param")

# --- 5. proposal-time checks ------------------------------------------------
class FakeBackend:
    def __init__(self, text):
        self.text = text

    def generate(self, messages, tools):
        return type("Turn", (), {"text": self.text})()


trace = [{"tool": "create_folder", "args": {"path": "/tmp/a"}, "result": {"ok": True}},
         {"tool": "list_files", "args": {"path": "/tmp/a"}, "result": {"ok": True}}]
proposal = skill_learning.generate_skill_definition_single(
    FakeBackend(json.dumps({**base_definition(), "chatter": "extra", "uncertain_params": [
        {"name": "base", "guessed_value": "/tmp", "question": "?"}, {"bogus": 1}]})), trace)
check(proposal is not None and proposal["name"] == "make_and_list_2", "colliding proposal name gets a free suffix")
check("chatter" not in proposal and len(proposal["uncertain_params"]) == 1,
      "unknown keys and malformed uncertain_params dropped")
bad = base_definition("uses_skill")
bad["steps"].append({"tool": "setup_project", "args": {"name": "{folder}"}})
check(skill_learning.generate_skill_definition_single(FakeBackend(json.dumps(bad)), trace) is None,
      "invalid proposal is never shown")
check(any(e["name"] == "uses_skill" for e in events("skill_proposal_rejected")), "... and is logged")
skill_trace = trace + [{"tool": "setup_project", "args": {"name": "x"}, "result": {"ok": True}}]
check(skill_learning.check_before_logging({"type": "response", "trace": skill_trace})
      is skill_learning.NOT_APPLICABLE, "a trace that used a skill isn't proposed at all")

# --- 6. template interpreter -------------------------------------------------
sub = template_skill._substitute
check(sub("{t}", {"t": "{x}"}) == "{x}", "a value containing {x} is not re-expanded")
check(sub("find {dir} -exec ls {} \\;", {"dir": "/tmp"}) == "find /tmp -exec ls {} \\;",
      "literal {} no longer blocks other placeholders")
check(sub("awk '{print $1}' {f}", {"f": "a.txt"}) == "awk '{print $1}' a.txt", "awk braces left alone")
check(sub("echo { {a}", {"a": "1"}) == "echo { 1", "a lone brace no longer raises")
check(sub("{{a}} {a}", {"a": "1"}) == "{a} 1", "{{ }} escapes to a literal brace")
check(sub("{a.__class__}", {"a": "1"}) == "{a.__class__}", "no attribute access")
check(sub("{a:>20}", {"a": "1"}) == "{a:>20}", "no format specs")
check(sub({"k": ["{a}", {"n": "x{a}"}]}, {"a": "1"}) == {"k": ["1", {"n": "x1"}]}, "lists and dicts recursed")
check(sub("{n}", {"n": 3}) == 3 and sub("n={n}", {"n": 3}) == "n=3", "whole placeholder keeps its type")

d = {**base_definition("bake"), "uncertain_params": [
    {"name": "base", "guessed_value": "/tmp/{folder}", "question": "?"}]}
baked = skill_learning.finalize_definition(d, {"base": False})
check(baked["steps"][0]["args"]["path"] == "/tmp/{{folder}}/{folder}", "baked literal braces are escaped")
check(sub(baked["steps"][0]["args"]["path"], {"folder": "x"}) == "/tmp/{folder}/x",
      "... so a guessed value can't become a placeholder")
d["uncertain_params"][0]["guessed_value"] = 42
check(skill_learning.finalize_definition(d, {"base": False})["steps"][0]["args"]["path"] == "42/{folder}",
      "non-string guessed value baked without crashing")

work = TMP / "work"
result = asyncio.run(registry.SKILLS["make_and_list"]["fn"]({"folder": "made"}, None, None))
check(result["ok"] and (work / "made").is_dir(), "learned skill runs end to end through dispatch()")
result = asyncio.run(registry.SKILLS["make_and_list"]["fn"]({}, None, None))
check(not result["ok"] and "folder" in result["error"] and result["steps"] == [],
      "missing parameter fails before any step runs")

# --- 7. loading: legacy migrate, edits, corrupt files -----------------------
legacy = base_definition("legacy_skill")
legacy_path = GEN / "legacy_skill.json"
legacy_path.write_text(json.dumps(legacy, indent=2))
os.utime(legacy_path, (1_700_000_000, 1_700_000_000))

(GEN / "broken.json").write_text("{not json")
(GEN / "wrong_tool.json").write_text(json.dumps({**base_definition("wrong_tool"),
                                                 "steps": [{"tool": "nope", "args": {}}]}))
(GEN / "mismatch.json").write_text(json.dumps(base_definition("something_else")))
(GEN / "run_command.json").write_text(json.dumps(base_definition("run_command")))
(GEN / "nan.json").write_text(json.dumps(base_definition("nan")).replace('"{base}/{folder}"', "NaN", 1))
os.symlink(legacy_path, GEN / "linked.json")
copied = json.loads(path.read_text())
copied["name"] = "zz_copy"
(GEN / "zz_copy.json").write_text(json.dumps(copied))

registry.load_generated_skills()
migrated = json.loads(legacy_path.read_text())
check("legacy_skill" in registry.SKILLS, "legacy file loads")
check({k: migrated[k] for k in legacy} == legacy, "legacy content unchanged by migration")
check(set(migrated) == set(legacy) | validate.METADATA_KEYS, "legacy file gained exactly the metadata")
check(migrated["created_at"] == 1_700_000_000, "legacy created_at taken from file mtime")
check(any(e["name"] == "legacy_skill" for e in events("skill_migrated")), "migration logged")
rejects = {Path(e["path"]).name: e["reason"] for e in events("skill_load_rejected")}
for name in ["broken.json", "wrong_tool.json", "mismatch.json", "run_command.json", "nan.json",
             "linked.json", "zz_copy.json"]:
    check(name in rejects, f"{name} skipped: {rejects.get(name)}")
check("make_and_list" in registry.SKILLS and registry.TOOLS["run_command"] is not None
      and registry.SKILLS.get("run_command") is None, "good skills still load; no tool shadowed")

before = len(events("skill_migrated"))
registry.load_generated_skills()
check(len(events("skill_migrated")) == before, "second load doesn't re-migrate")

edited = json.loads(legacy_path.read_text())
edited["steps"][1]["args"]["path"] = "{base}/{folder}/.."
legacy_path.write_text(json.dumps(edited, indent=2))
registry.load_generated_skills()
after_edit = json.loads(legacy_path.read_text())
check(after_edit["version"] == 2 and after_edit["content_hash"] == validate.content_hash(after_edit)
      and after_edit["id"] == migrated["id"], "hand-edited learned skill becomes version 2, same id")

imported = copy.deepcopy(after_edit)
imported["source"] = "imported:" + "ab" * 32
imported["steps"][1]["args"]["path"] = "{base}/{folder}"
legacy_path.write_text(json.dumps(imported, indent=2))
registry.load_generated_skills()
check("legacy_skill" not in registry.SKILLS, "imported skill whose content doesn't match its hash is rejected")

print(f"\nall {passed} checks passed")
