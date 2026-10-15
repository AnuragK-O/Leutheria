"""Check of the public skill registry pipeline (component A), all local.

    cd agent
    ./.venv/bin/python scripts/registry_pipeline_check.py

Covers: tool catalog export + staleness; validate.py importable with no
macOS/runtime deps and usable from the catalog alone; structural rejects
(bad hash, edit/delete of a published file, two files, path-traversal and
bad names, unknown tool, oversized, symlink, `source` field, duplicate keys,
someone else's name); the decision table; index build + sign + verify round
trip; tampered index / wrong key / tampered skill file all failing
verification; version assignment; yank. Runs skill-registry/tools/ as
subprocesses exactly as CI does, on temp copies, with a stub screener. No
network, no model, no sound, no sockets.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
REPO = AGENT_DIR.parent
REGISTRY = REPO / "skill-registry"
TOOLS = REGISTRY / "tools"
sys.path.insert(0, str(AGENT_DIR))

from agent.skills import validate  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="leutheria_registry_check_"))
CATALOG = AGENT_DIR / "tool_catalog.json"
passed = 0


def check(condition, label):
    global passed
    if not condition:
        print(f"FAIL  {label}")
        sys.exit(1)
    passed += 1


def py(*args, cwd=None):
    return subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, timeout=180, cwd=cwd)


def tool(script, *args):
    r = py(TOOLS / script, *args)
    # A failure has to be a decision, never a crash that happens to exit 1.
    check("Traceback" not in r.stderr, f"{script} did not crash: {r.stderr[-800:]}")
    return r


def skill(name="tidy_folder", steps=None, params=None, description="Make a dated folder in a place."):
    params = params if params is not None else {"place": {"type": "string", "description": "Where."}}
    steps = steps or [{"tool": "create_folder", "args": {"path": "{place}/archive"}}]
    d = {"name": name, "description": description,
         "input_schema": {"type": "object", "properties": params, "required": sorted(params)},
         "steps": steps}
    d.update({"id": str(uuid.uuid4()), "content_hash": validate.content_hash(d), "version": 1,
              "requires": validate.requires(d), "created_at": 1760000000})
    return d


def write(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) if not isinstance(value, str) else value, encoding="utf-8")
    return path


# --- 1. catalog ------------------------------------------------------------------

r = py(AGENT_DIR / "scripts" / "export_tool_catalog.py", "--check")
check(r.returncode == 0, f"committed tool_catalog.json is current: {r.stdout}{r.stderr}")
fresh = TMP / "catalog.json"
r = py(AGENT_DIR / "scripts" / "export_tool_catalog.py", "--out", fresh)
check(r.returncode == 0 and fresh.exists(), "exporter writes a catalog")
catalog = json.loads(fresh.read_text())
check(catalog["catalog_version"] == validate.catalog_digest(catalog), "catalog_version is the digest of the content")
check({"name", "safety", "input_schema", "paths", "outbound", "untrusted_output"} == set(catalog["tools"][0]),
      "catalog tool entries carry exactly the contract fields")
check("run_command" in {t["name"] for t in catalog["tools"]} and "setup_project" in catalog["builtin_skills"],
      "catalog lists tools and built-in skill names")
stale = json.loads(fresh.read_text())
next(t for t in stale["tools"] if t["name"] == "trash_file")["safety"] = "destructive"
write(TMP / "stale.json", stale)
r = py(AGENT_DIR / "scripts" / "export_tool_catalog.py", "--check", "--out", TMP / "stale.json")
check(r.returncode == 1 and "STALE" in r.stdout, "--check fails on a changed tool")
ref_only = dict(catalog, leutheria_ref="0" * 40)
write(TMP / "ref_only.json", ref_only)
r = py(AGENT_DIR / "scripts" / "export_tool_catalog.py", "--check", "--out", TMP / "ref_only.json")
check(r.returncode == 0, "--check ignores the exported-from ref")
try:
    validate.catalog_validation_kwargs(stale)
    check(False, "a catalog whose content doesn't match catalog_version is refused")
except validate.SkillValidationError:
    check(True, "")

# validate.py on Linux CI: stdlib only, nothing macOS or agent-runtime.
probe = ("import sys; sys.path.insert(0, sys.argv[1]); import agent.skills.validate as v; "
         "print(sorted(m for m in sys.modules if m.startswith('agent'))); "
         "print(sorted(m for m in sys.modules if m.split('.')[0] in "
         "{'objc','AppKit','Quartz','Foundation','numpy','sounddevice','websockets','anthropic','dotenv'}))")
r = py("-I", "-c", probe, AGENT_DIR)
lines = r.stdout.strip().splitlines()
check(r.returncode == 0 and lines[0] == "['agent', 'agent.skills', 'agent.skills.template_skill', 'agent.skills.validate']"
      and lines[1] == "[]", f"validate.py imports only stdlib + template_skill: {r.stdout}{r.stderr}")

kw = validate.catalog_validation_kwargs(catalog)
good = skill()
validate.validate_shared(good, **kw)
check(True, "validate_shared accepts a well-formed shared skill from the catalog alone")
for label, bad in [
    ("a `source` field", {**good, "source": "learned"}),
    ("a wrong content_hash", {**good, "content_hash": "0" * 64}),
    ("a missing id", {k: v for k, v in good.items() if k != "id"}),
    ("a built-in skill's name", {**good, "name": "setup_project"}),
    ("a skill as a step", {**skill(steps=[{"tool": "setup_project", "args": {}}], params={})}),
]:
    try:
        validate.validate_shared(bad, **kw)
        check(False, f"validate_shared refuses {label}")
    except validate.SkillValidationError:
        check(True, "")

# --- 2. keys ---------------------------------------------------------------------

keys = TMP / "keys.json"
key_a, key_b = TMP / "a.pem", TMP / "b.pem"
r = tool("keygen.py", "--private-out", key_a, "--keys", keys)
check(r.returncode == 0 and oct(key_a.stat().st_mode & 0o777) == "0o600", "keygen writes a 0600 private key")
check(tool("keygen.py", "--private-out", key_a).returncode == 1, "keygen refuses to overwrite a key")
tool("keygen.py", "--private-out", key_b)
entry_a = json.loads(keys.read_text())["keys"][0]
check(len(entry_a["key_id"]) == 16, "public entry has a 16-hex key_id")

# --- 3. pipeline -----------------------------------------------------------------

stub = write(TMP / "stub" / "screen.py", '''import json, sys, pathlib
args = sys.argv[1:]
mode = (pathlib.Path(__file__).parent / "mode.txt").read_text().strip()
out = args[args.index("--out") + 1]
assert pathlib.Path(args[0]).is_file() and pathlib.Path(args[args.index("--catalog") + 1]).is_file()
if mode == "crash":
    sys.exit(3)
verdict, risk, static, match = {"approve": ("approve", "low", "clean", True), "flag": ("flag", "medium", "clean", True),
    "reject": ("reject", "high", "clean", True), "static": ("approve", "low", "needs_review", True),
    "high": ("approve", "high", "clean", True), "mismatch": ("approve", "low", "clean", False)}[mode]
json.dump({"verdict": verdict, "risk": risk, "findings": [{"where": "step 0", "issue": "x @maintainer", "severity": "low"}],
  "summary": "stub", "description_matches_steps": match,
  "static": {"verdict": static, "findings": [{"rule": "sudo", "severity": "high"}] if static != "clean" else []},
  "model": "stub-model", "screen_version": 1}, open(out, "w"))
''')
WORK = TMP / "work"


def submit(definition_or_text, author="alice", mode="approve", path=None, human=False, now="2026-10-15T10:00:00Z"):
    (TMP / "stub" / "mode.txt").write_text(mode)
    f = write(TMP / "submission.json", definition_or_text)
    args = ["submit", "--work", WORK, "--leutheria", REPO, "--skill", f, "--author", author,
            "--key-file", key_a, "--keys", keys, "--screen-script", stub, "--now", now]
    if path:
        args += ["--path", path]
    if human:
        args.append("--human-approve")
    r = tool("local_pipeline.py", *args)
    try:
        return json.loads(r.stdout), r
    except ValueError:
        print(r.stdout, r.stderr)
        raise


def index():
    return json.loads((WORK / "repo" / "index.json").read_text())


v1 = skill()
s, r = submit(v1)
check(r.returncode == 0 and s["decision"]["action"] == "merge" and s.get("verified"), f"clean submission merges, publishes, verifies: {s}")
e = index()["skills"][0]
check((e["name"], e["version"], e["has_run_command"], e["author"]) == ("tidy_folder", 1, False, "alice"), "v1 entry")
check(e["review"] == {"verdict": "auto_approved", "risk": "low", "flags": ["low step 0: x maintainer"],
                      "screened_by": "stub-model", "screen_version": 1}, f"review block from the screener: {e['review']}")
check(e["path"] == f"skills/tidy_folder/{v1['content_hash']}.json" and len(e["sha256"]) == 64 and e["size"] > 0
      and e["summary"] == v1["description"] and e["requires"] == ["create_folder"], "entry fields")
check(index()["catalog_version"] == json.loads(CATALOG.read_text())["catalog_version"], "index carries catalog_version")

v2 = skill(steps=[{"tool": "create_folder", "args": {"path": "{place}/archive"}},
                  {"tool": "run_command", "args": {"cmd": "ls {place}"}}],
           description="Make a folder, then **list** it. See https://example.com")
s, _ = submit(v2, now="2026-10-15T11:00:00Z")
names = [(x["name"], x["version"], x["has_run_command"]) for x in index()["skills"]]
check(names == [("tidy_folder", 1, False), ("tidy_folder", 2, True)], f"second version gets v2: {names}")
check(index()["skills"][1]["summary"] == "Make a folder, then list it. See", "summary strips markdown and URLs")

s, _ = submit(skill(steps=[{"tool": "get_current_datetime", "args": {}}], params={}), author="mallory")
check(s["decision"]["action"] == "close" and any("someone else" in x for x in s["structural"]["errors"]),
      "another author can't publish a new version of a taken name")

rejects = {
    "bad hash": ({**v1, "content_hash": "f" * 64, "id": str(uuid.uuid4())}, None),
    "name/folder mismatch": (skill(name="other_name"), f"skills/tidy_folder/{skill(name='other_name')['content_hash']}.json"),
    "unknown tool": (skill(name="ghost", steps=[{"tool": "format_disk", "args": {}}], params={}), None),
    "a `source` field": ({**skill(name="sourced"), "source": "learned"}, None),
    "path traversal": (skill(name="climber"), "skills/../climber.json"),
    "bad folder name": (skill(name="climber"), f"skills/Climber/{skill(name='climber')['content_hash']}.json"),
    "too deep": (skill(name="climber"), f"skills/climber/x/{skill(name='climber')['content_hash']}.json"),
    "file name not the hash": (skill(name="climber"), "skills/climber/latest.json"),
}
for label, (definition, path) in rejects.items():
    s, _ = submit(definition, path=path)
    check(s["decision"]["action"] == "close" and not s["structural"]["ok"], f"structural reject: {label}: {s['structural']}")
big = skill(name="bloated")
s, _ = submit(json.dumps(big) + " " * 70_000)
check(s["decision"]["action"] == "close" and "cap" in " ".join(s["structural"]["errors"]), "oversized file rejected")
dup = skill(name="twice")
text = json.dumps(dup)
s, _ = submit(text[:-1] + ', "description": "something else"}', path=f"skills/twice/{dup['content_hash']}.json")
check(s["decision"]["action"] == "close" and "duplicate key" in " ".join(s["structural"]["errors"]), "duplicate JSON keys rejected")

# Edits, deletes, two files, symlinks: built by hand against the published repo.
repo = WORK / "repo"
published = repo / index()["skills"][0]["path"]


def struct(mutate):
    head = TMP / "head"
    shutil.rmtree(head, ignore_errors=True)
    shutil.copytree(repo, head, symlinks=True)
    mutate(head)
    r = tool("check_submission.py", "--base", repo, "--head", head, "--leutheria", REPO, "--author", "alice",
             "--out", TMP / "s.json")
    return r.returncode, json.loads((TMP / "s.json").read_text())


def add(head, d):
    write(head / "skills" / d["name"] / f"{d['content_hash']}.json", d)


rc, res = struct(lambda h: (h / published.relative_to(repo)).write_text(published.read_text() + " "))
check(rc == 1 and any("changes" in x for x in res["errors"]), "editing a published file rejected")
rc, res = struct(lambda h: (h / published.relative_to(repo)).unlink())
check(rc == 1 and any("deletes" in x for x in res["errors"]), "deleting a published file rejected")
rc, res = struct(lambda h: (add(h, skill(name="one_a")), add(h, skill(name="one_b"))))
check(rc == 1 and any("exactly one" in x for x in res["errors"]), "two files in one PR rejected")
rc, res = struct(lambda h: (add(h, skill(name="one_a")), write(h / "tools" / "evil.py", "x")))
check(rc == 1 and any("outside skills/" in x for x in res["errors"]), "a file outside skills/ rejected")
rc, res = struct(lambda h: (h / "index.json").write_text("{}"))
check(rc == 1, "a PR touching index.json rejected")


def link(h):
    target = h / "skills" / "linked" / f"{skill(name='linked')['content_hash']}.json"
    target.parent.mkdir(parents=True)
    os.symlink(published, target)


rc, res = struct(link)
check(rc == 1 and any("symlink" in x for x in res["errors"]), "a symlinked skill file rejected")
rc, res = struct(lambda h: add(h, skill(name="fine_one")))
check(rc == 0 and res["ok"] and res["name"] == "fine_one", "a clean hand-built submission passes")

# Decision table.
for mode, action in [("flag", "review"), ("static", "review"), ("high", "review"), ("mismatch", "review"),
                     ("reject", "close"), ("crash", "review")]:
    s, _ = submit(skill(name=f"mode_{mode}"), mode=mode)
    check(s["decision"]["action"] == action, f"screener {mode} -> {action}: {s['decision']}")
s, _ = submit(skill(name="mode_flag"), mode="flag", human=True, now="2026-10-15T12:00:00Z")
check(s["decision"]["action"] == "merge" and s.get("verified"), "maintainer approval merges a flagged skill")
hv = [x for x in index()["skills"] if x["name"] == "mode_flag"][0]
check(hv["review"]["verdict"] == "human_approved" and hv["version"] == 1, "human_approved recorded")
s, _ = submit(skill(name="mode_bad", steps=[{"tool": "nope", "args": {}}], params={}), human=True)
check(s["decision"]["action"] == "close", "maintainer approval can't override a structural failure")

# --- 4. build / sign / verify ----------------------------------------------------

check(tool("verify_index.py", "--repo", repo, "--keys", keys, "--leutheria", REPO).returncode == 0, "published repo verifies")
r = tool("build_index.py", "--repo", repo, "--leutheria", REPO)
check(r.returncode == 0 and "unchanged" in r.stdout, "rebuilding an unchanged registry leaves index.json alone")
pending = skill(name="pushed_directly")
write(repo / "skills" / "pushed_directly" / f"{pending['content_hash']}.json", pending)
r = tool("build_index.py", "--repo", repo, "--leutheria", REPO, "--out", TMP / "i.json")
check(r.returncode == 0 and "pending" in r.stdout and "pushed_directly" not in (TMP / "i.json").read_text(),
      "a skill file without a review record is not indexed")
shutil.rmtree(repo / "skills" / "pushed_directly")


def tampered(mutate, label):
    copy = TMP / "tamper"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(repo, copy)
    mutate(copy)
    r = tool("verify_index.py", "--repo", copy, "--keys", keys, "--leutheria", REPO)
    check(r.returncode == 1, f"verification fails: {label}: {r.stdout}{r.stderr}")
    return r.stderr


tampered(lambda c: (c / "index.json").write_text((c / "index.json").read_text().replace('"risk": "low"', '"risk": "lol"', 1)),
         "index edited after signing")
tampered(lambda c: (c / "index.json").write_bytes((c / "index.json").read_bytes() + b"\n"), "one byte appended to the index")
tampered(lambda c: tool("sign_index.py", "--index", c / "index.json", "--out", c / "index.json.sig", "--key-file", key_b),
         "signed with a key not in the list")


def forged_key_id(c):
    sig = json.loads((c / "index.json.sig").read_text())
    tool("sign_index.py", "--index", c / "index.json", "--out", c / "index.json.sig", "--key-file", key_b)
    forged = json.loads((c / "index.json.sig").read_text())
    write(c / "index.json.sig", {"key_id": sig["key_id"], "sig": forged["sig"]})


tampered(forged_key_id, "wrong key's signature under the right key_id")
tampered(lambda c: write(c / "index.json.sig", {"key_id": entry_a["key_id"], "sig": "AAAA"}), "garbage signature")
tampered(lambda c: (c / "index.json.sig").unlink(), "missing signature")


def edit_skill_file(c):
    f = c / index()["skills"][0]["path"]
    d = json.loads(f.read_text())
    d["steps"][0]["args"]["path"] = "~/Library/LaunchAgents"
    f.write_text(json.dumps(d, indent=2))


tampered(edit_skill_file, "a skill file changed after indexing")


def swap_description(c):
    f = c / index()["skills"][0]["path"]
    f.write_text(f.read_text().replace("Make a dated folder", "Make a dated foldr"))


tampered(swap_description, "prose-only edit (content_hash unchanged) still caught by sha256")
other_keys = write(TMP / "other_keys.json", {"keys": [json.loads(tool("keygen.py", "--private-out", TMP / "c.pem").stdout)]})
r = tool("verify_index.py", "--repo", repo, "--keys", other_keys)
check(r.returncode == 1 and "unknown signing key" in r.stderr, "client with a different key refuses the index")

# --- 5. yank ---------------------------------------------------------------------

y = ["yank", "--work", WORK, "--leutheria", REPO, "--key-file", key_a, "--keys", keys, "--name", "tidy_folder",
     "--content-hash", v1["content_hash"], "--reason", "superseded"]
r = tool("local_pipeline.py", *y)
s = json.loads(r.stdout)
check(r.returncode == 0 and s["yanked"] and s["verified"], f"yank publishes and verifies: {s}")
ys = {(x["version"], x["yanked"]) for x in index()["skills"] if x["name"] == "tidy_folder"}
check(ys == {(1, True), (2, False)}, f"only the yanked version is marked: {ys}")
check(tool("local_pipeline.py", *y).returncode == 1, "yanking twice refused")
y[y.index("--content-hash") + 1] = "a" * 64
check(tool("local_pipeline.py", *y).returncode == 1, "yanking an unpublished version refused")
y[y.index("--name") + 1] = "../etc"
check(tool("local_pipeline.py", *y).returncode == 1, "yank refuses a path-like name")
s, _ = submit(skill(description="Same steps, new words."))
check(s["decision"]["action"] == "close" and any("changes" in x for x in s["structural"]["errors"]),
      "a prose-only resubmission (same content_hash, same path) is an edit, rejected")
s, _ = submit(skill(steps=[{"tool": "create_folder", "args": {"path": "{place}/old"}}]), now="2026-10-15T13:00:00Z")
check([x["version"] for x in index()["skills"] if x["name"] == "tidy_folder"] == [1, 2, 3],
      "versions keep counting after a yank")

# --- 6. workflows ----------------------------------------------------------------

workflows = sorted((REGISTRY / ".github" / "workflows").glob("*.yml"))
check([w.name for w in workflows] == ["publish.yml", "submission.yml", "yank.yml"], "three workflows")
try:
    import yaml  # noqa: F401

    parsed = {w.name: yaml.safe_load(w.read_text()) for w in workflows}
except ImportError:
    ruby = shutil.which("ruby")
    check(ruby is not None, "a YAML parser is available (PyYAML or ruby)")
    r = subprocess.run([ruby, "-ryaml", "-rjson", "-e",
                        "puts JSON.dump(ARGV.to_h { |f| [File.basename(f), YAML.safe_load(File.read(f))] })",
                        *map(str, workflows)], capture_output=True, text=True, timeout=30)
    check(r.returncode == 0, f"workflow YAML parses: {r.stderr}")
    parsed = json.loads(r.stdout)
for name, doc in parsed.items():
    check(isinstance(doc, dict) and doc.get("jobs"), f"{name} has jobs")
for w in workflows:
    text = w.read_text()
    for ref in [part.split()[0] for part in text.split("tools/")[1:]]:
        if ref.endswith(".py"):
            check((TOOLS / ref).is_file(), f"{w.name} calls tools/{ref}, which exists")
sub = (REGISTRY / ".github" / "workflows" / "submission.yml").read_text()
check("pull_request_target" in sub and "persist-credentials: false" in sub and "--match-head-sha" in sub,
      "submission workflow: base scripts, PR as data, merges only the screened head")
check("${{ github.event.pull_request.title" not in sub and "${{ github.event.pull_request.body" not in sub,
      "no PR text interpolated into shell")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")
