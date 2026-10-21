"""Check of the public skill registry pipeline (component A), all local.

    cd agent
    ./.venv/bin/python scripts/registry_pipeline_check.py

Covers: tool catalog export + staleness; validate.py importable with no
macOS/runtime deps and usable from the catalog alone; structural rejects
(bad hash, edit/delete of a published file, two files, path-traversal and
bad names, unknown tool, oversized, symlink, `source` field, duplicate keys,
someone else's name); the decision table; index build + sign + verify round
trip; tampered index / wrong key / tampered skill file all failing
verification; version assignment; yank; a burst of concurrent merges racing
to record and publish against a local bare repo (record_push.py,
commit_index.py; REGISTRY_BURST=<n> sets its size, default 40). Runs skill-registry/tools/ as
subprocesses exactly as CI does, on temp copies, with a stub screener. No
network, no model, no sound, no sockets.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
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
    """Any FAIL ends the run with exit 1. From a worker thread (section 4b runs a pool)
    sys.exit would end only that thread and the run could still finish "all passed", so
    there the process is ended outright. (Reading the exit status through a pipe --
    `... | tail` -- reports tail's status, not this one; use pipefail or PIPESTATUS.)"""
    global passed
    if not condition:
        print(f"FAIL  {label}", flush=True)
        if threading.current_thread() is not threading.main_thread():
            sys.stderr.flush()
            os._exit(1)
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
    "high": ("approve", "high", "clean", True), "mismatch": ("approve", "low", "clean", False),
    "static_reject": ("reject", "high", "reject", True), "no_static": ("approve", "low", None, True)}[mode]
json.dump({"verdict": verdict, "risk": risk, "findings": [{"where": "step 0", "issue": "x @maintainer", "severity": "low"}],
  "summary": "stub", "description_matches_steps": match,
  "static": {"verdict": static, "findings": [{"rule": "sudo", "severity": "high"}] if static != "clean" else []},
  "model": {"id": "stub-model", "called": True, "status": "ok", "error": None, "usage": None}, "screen_version": 1}, open(out, "w"))
''')
WORK = TMP / "work"
NOW = "2026-10-16T00:00:00Z"  # verification clock: after every --now below, before their indexes expire


def submit(definition_or_text, author="alice", mode="approve", path=None, human=False, now="2026-10-15T10:00:00Z",
           override=False):
    (TMP / "stub" / "mode.txt").write_text(mode)
    f = write(TMP / "submission.json", definition_or_text)
    args = ["submit", "--work", WORK, "--leutheria", REPO, "--skill", f, "--author", author,
            "--key-file", key_a, "--keys", keys, "--screen-script", stub, "--now", now]
    if path:
        args += ["--path", path]
    if human:
        args.append("--human-approve")
    if override:
        args.append("--override-static-reject")
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

# H4: a maintainer approval never overrides a static block by habit, and needs a static result at all.
s, _ = submit(skill(name="mode_blocked"), mode="static_reject", human=True)
check(s["decision"]["action"] == "close" and "override" in s["decision"]["comment"],
      f"maintainer approval alone can't merge a static reject: {s['decision']}")
s, _ = submit(skill(name="mode_nostatic"), mode="no_static", human=True)
check(s["decision"]["action"] == "review", f"maintainer approval with no static result goes to review: {s['decision']}")
s, _ = submit(skill(name="mode_blocked"), mode="static_reject", human=True, override=True, now="2026-10-15T12:30:00Z")
check(s["decision"]["action"] == "merge" and "overridden" in " ".join(s["decision"]["reasons"]) and s.get("verified"),
      f"the separately named override merges a static reject: {s['decision']}")
r = tool("decide.py", "--structural", WORK / "structural.json", "--override-static-reject", "--out", TMP / "d.json")
check(r.returncode == 2, "--override-static-reject without a verified approval is a usage error")

# H4: approval = an APPROVED review on exactly the head commit, by a writer who isn't the author.
HEAD, OLD = "a" * 40, "b" * 40


def review(login, state, commit, at):
    return {"id": hash((login, at)) & 0xFFFF, "user": {"login": login}, "state": state,
            "commit_id": commit, "submitted_at": at}


def approval(reviews, perms="maint write\nhelper triage\n", author="alice", head=HEAD):
    write(TMP / "reviews.json", reviews)
    (TMP / "perms.txt").write_text(perms)
    r = tool("human_approval.py", "verify", "--reviews", TMP / "reviews.json", "--head-sha", head,
             "--author", author, "--permissions", TMP / "perms.txt", "--out", TMP / "human.json")
    check(r.returncode == 0, f"human_approval decides: {r.stderr}")
    return json.loads((TMP / "human.json").read_text())


check(approval([review("maint", "APPROVED", HEAD, "2026-10-15T10:00:00Z")])["approved"], "approval on the head counts")
check(not approval([review("maint", "APPROVED", OLD, "2026-10-15T10:00:00Z")])["approved"],
      "approval of an earlier commit doesn't count for a new head")
check(not approval([review("alice", "APPROVED", HEAD, "2026-10-15T10:00:00Z")], perms="alice admin\n")["approved"],
      "the author can't approve their own submission")
check(not approval([review("helper", "APPROVED", HEAD, "2026-10-15T10:00:00Z")])["approved"],
      "a reviewer without write access doesn't count (triage can label, not approve)")
check(not approval([review("maint", "APPROVED", HEAD, "2026-10-15T10:00:00Z"),
                    review("maint", "CHANGES_REQUESTED", HEAD, "2026-10-15T11:00:00Z")])["approved"],
      "a later request for changes withdraws the approval")
check(not approval([review("maint", "APPROVED", HEAD, "2026-10-15T10:00:00Z"),
                    review("maint", "DISMISSED", HEAD, "2026-10-15T11:00:00Z")])["approved"],
      "a dismissed approval doesn't count")
check(approval([[review("x", "COMMENTED", HEAD, "2026-10-15T09:00:00Z")],
                [review("maint", "APPROVED", HEAD, "2026-10-15T10:00:00Z"),
                 review("maint", "COMMENTED", OLD, "2026-10-15T11:00:00Z")]])["approved"],
      "paged review lists are read; a later comment doesn't withdraw an approval")
check(not approval([], perms="")["approved"], "no reviews, no approval")
write(TMP / "reviews.json", [])
check(tool("human_approval.py", "candidates", "--reviews", TMP / "reviews.json", "--head-sha", "HEAD",
           "--author", "alice").returncode == 2, "a head that isn't a full sha is refused")

# --- 4. build / sign / verify ----------------------------------------------------

check(tool("verify_index.py", "--repo", repo, "--keys", keys, "--leutheria", REPO, "--now", NOW).returncode == 0,
      "published repo verifies")
r = tool("build_index.py", "--repo", repo, "--leutheria", REPO)
check(r.returncode == 0 and "unchanged" in r.stdout, "rebuilding an unchanged registry leaves index.json alone")
pending = skill(name="pushed_directly")
write(repo / "skills" / "pushed_directly" / f"{pending['content_hash']}.json", pending)
r = tool("build_index.py", "--repo", repo, "--leutheria", REPO, "--out", TMP / "i.json")
check(r.returncode == 0 and "pending" in r.stdout and "pushed_directly" not in (TMP / "i.json").read_text(),
      "a skill file without a review record is not indexed")
shutil.rmtree(repo / "skills" / "pushed_directly")

# M1: two PRs claim one new name from the same base. Both pass the structural check (it reads
# the base); the second must be refused at record time, against main as it is then.
race = TMP / "race"
shutil.rmtree(race, ignore_errors=True)
shutil.copytree(repo, race)
claim_b = skill(name="race_name")
claim_c = skill(name="race_name", steps=[{"tool": "create_folder", "args": {"path": "{place}/other"}}])
for who, d in (("bob", claim_b), ("carol", claim_c)):
    rc, res = struct(lambda h, d=d: add(h, d))  # struct() checks as alice; the name is new, so author is moot
    check(rc == 0, f"{who}'s claim passes the structural check against the shared base")
rel_b = f"skills/race_name/{claim_b['content_hash']}.json"
rel_c = f"skills/race_name/{claim_c['content_hash']}.json"
add(race, claim_b)
check(tool("record_review.py", "--repo", race, "--path", rel_b, "--verdict", "auto_approved",
           "--author", "bob").returncode == 0, "first claim of a new name is recorded")
r = tool("record_review.py", "--repo", race, "--path", rel_c, "--verdict", "auto_approved", "--author", "carol",
         "--check-owner-only")
check(r.returncode == 1 and "someone else" in r.stderr, "the pre-merge owner check refuses the second claimant")
check(tool("record_review.py", "--repo", race, "--path", rel_c, "--verdict", "auto_approved", "--author", "bob",
           "--check-owner-only").returncode == 0, "the pre-merge owner check lets the owner through")
add(race, claim_c)
r = tool("record_review.py", "--repo", race, "--path", rel_c, "--verdict", "auto_approved", "--author", "carol")
check(r.returncode == 1 and "someone else" in r.stderr and not (race / "reviews" / "race_name" /
      f"{claim_c['content_hash']}.json").exists(), "record_review refuses a second author for a name on main")
write(race / "reviews" / "race_name" / f"{claim_c['content_hash']}.json",
      {**json.loads((race / "reviews" / "race_name" / f"{claim_b['content_hash']}.json").read_text()),
       "content_hash": claim_c["content_hash"], "version": 2, "author": "carol"})
r = tool("build_index.py", "--repo", race, "--leutheria", REPO, "--out", TMP / "race.json")
check(r.returncode == 1 and "more than one author" in r.stderr, f"build_index fails on two authors for one name: {r.stderr}")

# --- 4b. burst: many merges at once racing to record and publish -----------------
# A local bare repo is "main". Each worker does what one submission run does after its
# merge decision: merge (push the skill file), tools/record_push.py, then a publish
# (build_index, sign_index, tools/commit_index.py) -- all in parallel, with no
# serialisation at all (the worst case; the workflow's concurrency group only coalesces).
# The old workflow loop (3 tries, no backoff) stranded submissions under exactly this.
import concurrent.futures  # noqa: E402
import random  # noqa: E402
import time  # noqa: E402

BURST = int(os.environ.get("REGISTRY_BURST", "40"))
BURST_NAMES = 6
GITC = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def g(cwd, *args, check_rc=True):
    r = subprocess.run(["git", "-C", str(cwd), *GITC, *args], capture_output=True, text=True, timeout=120)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr}")
    return r


def fresh_clone(bare_repo, dest):
    # Under 40+ concurrent pushes a local clone occasionally fails (exit 128, seen once,
    # not reproduced): retry, so the scenario tests the tools, not the harness.
    for n in range(20):
        shutil.rmtree(dest, ignore_errors=True)
        r = subprocess.run(["git", "clone", "--quiet", "--branch", "main", str(bare_repo), str(dest)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            break
        time.sleep(0.05 * (n + 1))
    else:
        raise RuntimeError(f"git clone {bare_repo}: {r.stderr}")
    for k, v in (("user.name", "t"), ("user.email", "t@example.invalid")):
        g(dest, "config", k, v)
    return dest


def push_change(bare_repo, dest, rel, content):
    """Stands for a server-side change to main (gh pr merge, a maintainer push): retried until it lands."""
    for _ in range(1000):
        fresh_clone(bare_repo, dest)
        write(dest / rel, content)
        g(dest, "add", rel)
        g(dest, "commit", "--quiet", "-m", f"change {rel}")
        if g(dest, "push", "--quiet", "origin", "HEAD:main", check_rc=False).returncode == 0:
            return
        time.sleep(random.uniform(0, 0.05))
    raise RuntimeError(f"could not push {rel}")


def publish_run(bare_repo, dest):
    clone = fresh_clone(bare_repo, dest)
    built = g(clone, "rev-parse", "HEAD").stdout.strip()
    b = py(TOOLS / "build_index.py", "--repo", clone, "--leutheria", REPO)
    if b.returncode != 0:
        return f"build failed: {b.stderr}"
    s = py(TOOLS / "sign_index.py", "--index", clone / "index.json", "--out", clone / "index.json.sig", "--key-file", key_a)
    if s.returncode != 0:
        return f"sign failed: {s.stderr}"
    c = py(TOOLS / "commit_index.py", "--repo", clone, "--built-sha", built, "--index", clone / "index.json",
           "--sig", clone / "index.json.sig", "--base-delay", "0.02", "--max-delay", "0.2", "--attempts", "50")
    return c.stdout.strip() if c.returncode == 0 else f"commit failed: {c.stderr}"


def record_push(bare_repo, work, rel, author, pr, *extra):
    return py(TOOLS / "record_push.py", "--remote", bare_repo, "--work", work, "--path", rel,
              "--verdict", "auto_approved", "--author", author, "--pr", pr, *extra)


BT = TMP / "burst"
seed = BT / "seed"
shutil.copytree(REGISTRY, seed, ignore=shutil.ignore_patterns(".git", "__pycache__", "index.json", "index.json.sig"))
subprocess.run(["git", "init", "--quiet", "-b", "main", str(seed)], check=True, timeout=60)
g(seed, "add", "-A")
g(seed, "commit", "--quiet", "-m", "seed")
bare = BT / "main.git"
subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(bare)], check=True, timeout=60)

burst = []  # (i, name, author, definition, rel)
for i in range(BURST):
    name, author = f"burst_{i % BURST_NAMES}", f"Author{i % BURST_NAMES}"
    d = skill(name=name, steps=[{"tool": "create_folder", "args": {"path": f"{{place}}/v{i}"}}])
    burst.append((i, name, author, d, f"skills/{name}/{d['content_hash']}.json"))


# A version published before the burst, which maintainers yank DURING it: three yank runs at
# once (the security response, dispatched twice by two maintainers plus a re-run), racing the
# record pushes. Each must succeed (one yanks, the others find it already yanked).
YANK_REL_D = skill(name="yank_target", steps=[{"tool": "create_folder", "args": {"path": "{place}/bad"}}])
YANK_REL = f"skills/yank_target/{YANK_REL_D['content_hash']}.json"
YANKS = 3


def yank_push(bare_repo, work, name, digest, *extra):
    return py(TOOLS / "yank_push.py", "--remote", bare_repo, "--work", work, "--name", name,
              "--content-hash", digest, "--reason", "malicious", "--by", "maint", *extra)


push_change(bare, BT / "merge" / "yank_target", YANK_REL, YANK_REL_D)
check(record_push(bare, BT / "rec" / "yank_target", YANK_REL, "Yanky", "500").returncode == 0,
      "burst: the version to be yanked is published first")

MERGE_LOCK = threading.Lock()  # gh pr merge is atomic on GitHub's side: merges don't race each other


def submission_run(item):
    if item[0] == "yank":
        time.sleep(random.uniform(0, 1.0))
        y = yank_push(bare, BT / "yank" / str(item[1]), "yank_target", YANK_REL_D["content_hash"],
                      "--base-delay", "0.02", "--max-delay", "0.3", "--attempts", "400", "--deadline", "300")
        return ("yank", item[1]), y, publish_run(bare, BT / "pub" / f"y{item[1]}")  # yank.yml calls publish
    i, name, author, d, rel = item
    time.sleep(random.uniform(0, 0.5))                                  # screening ends at slightly different times
    with MERGE_LOCK:
        push_change(bare, BT / "merge" / str(i), rel, d)                 # gh pr merge
    r = record_push(bare, BT / "rec" / str(i), rel, author.lower() if (i // BURST_NAMES) % 2 else author, str(i),
                    "--base-delay", "0.02", "--max-delay", "0.3", "--attempts", "400", "--deadline", "300")
    return i, r, publish_run(bare, BT / "pub" / str(i))                 # publish (called)


started = time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=BURST + YANKS) as pool:
    results = list(pool.map(submission_run, burst + [("yank", k) for k in range(YANKS)]))
elapsed = time.monotonic() - started
yank_results = [r for i, r, _ in results if isinstance(i, tuple)]
bad_y = [(r.returncode, r.stderr[-400:]) for r in yank_results if r.returncode != 0 or "Traceback" in r.stderr]
check(not bad_y, f"burst: every concurrent yank_push succeeds: {bad_y[:3]}")
# Usually one says "yanked" and the rest "already yanked"; two that build their commit from
# the same main in the same second make byte-identical commits (same parent, tree, message,
# time), so the second push is "up to date" and both say "yanked". Either way main gets the
# yank exactly once -- asserted on main below, which is what matters.
check(all(r.stdout.strip().rsplit(" ", 1)[0] in ("yanked", "already yanked") for r in yank_results)
      and any(r.stdout.startswith("yanked") for r in yank_results),
      f"burst: the yank lands and the other runs succeed too: {[r.stdout for r in yank_results]}")
bad = [(i, r.returncode, r.stderr[-400:]) for i, r, _ in results
       if not isinstance(i, tuple) and (r.returncode != 0 or "Traceback" in r.stderr)]
check(not bad, f"burst of {BURST}: every record_push succeeds: {bad[:3]}")
retried = sum(r.stderr.count("retrying") for _, r, _ in results)
check(retried > 0, f"burst of {BURST}: the scenario really raced (pushes were rejected and retried): {retried}")
pub_bad = [(i, p) for i, _, p in results if not any(w in p for w in ("committed", "unchanged", "superseded"))]
check(not pub_bad, f"burst: every publish run commits, finds nothing new, or steps aside: {pub_bad[:3]}")

final = fresh_clone(bare, BT / "final")
records = sorted((final / "reviews").glob("*/*.json"))
check(len(records) == BURST + 1, f"burst: exactly one record per submission ({len(records)} for {BURST} + 1)")
by_name = {}
for i, name, author, d, rel in burst:
    p = final / "reviews" / name / f"{d['content_hash']}.json"
    rec = json.loads(p.read_text()) if p.exists() else {}
    check(rec.get("pr") == i and rec.get("author", "").lower() == author.lower() and rec.get("name") == name,
          f"burst: submission {i} has its own record: {rec}")
    by_name.setdefault(name, []).append(rec["version"])
for name, versions in sorted(by_name.items()):
    check(sorted(versions) == list(range(1, len(versions) + 1)),
          f"burst: {name}'s versions are unique and contiguous: {sorted(versions)}")
log = g(final, "log", "--format=%s").stdout.splitlines()
check(sum(1 for s in log if s.startswith("Review record for")) == BURST + 1, "burst: one review-record commit per submission")
check(sum(1 for s in log if s.startswith("Yank yank_target")) == 1, "burst: one yank commit for three concurrent yanks")
yanked_doc = json.loads((final / "yanked.json").read_text())
check([(y["name"], y["content_hash"]) for y in yanked_doc["yanked"]] == [("yank_target", YANK_REL_D["content_hash"])],
      f"burst: yanked.json on main holds the yank exactly once: {yanked_doc}")
check((final / "index.json").is_file(), f"burst: an index was published: {sorted({p for _, _, p in results})}")
idx = json.loads((final / "index.json").read_text())
check({(e["name"], e["content_hash"], e["version"]) for e in idx["skills"]} ==
      {(n, d["content_hash"], json.loads((final / "reviews" / n / f"{d['content_hash']}.json").read_text())["version"])
       for _, n, _, d, _ in burst} | {("yank_target", YANK_REL_D["content_hash"], 1)},
      f"burst: the index left on main after the burst lists all {BURST} submissions ({len(idx['skills'])})")
check([e["yanked"] for e in idx["skills"] if e["name"] == "yank_target"] == [True]
      and not any(e["yanked"] for e in idx["skills"] if e["name"] != "yank_target"),
      "burst: that index marks the yanked version, and only it")
check(tool("verify_index.py", "--repo", final, "--keys", keys, "--leutheria", REPO).returncode == 0,
      "burst: that index verifies")
r = tool("build_index.py", "--repo", final, "--leutheria", REPO, "--out", BT / "rebuilt.json")
check(r.returncode == 0 and "pending" not in r.stdout, f"burst: nothing left pending: {r.stdout}")
print(f"      burst of {BURST}: {retried} rejected pushes retried, {elapsed:.1f}s")

# Re-running is safe: a record already on main for this file and author is success, not a second record.
head = g(final, "rev-parse", "HEAD").stdout.strip()
i0, n0, a0, d0, rel0 = burst[0]
report = BT / "report.md"
r = record_push(bare, BT / "rec" / "again", rel0, a0, str(i0), "--report", report)
check(r.returncode == 0 and "already recorded" in r.stdout and not report.exists() and
      g(bare, "rev-parse", "main").stdout.strip() == head, f"record_push re-run is a no-op success: {r.stdout}{r.stderr}")

# A name claimed by someone else on main is refused at once, not retried, and says what to do.
intruder = skill(name=n0, steps=[{"tool": "create_folder", "args": {"path": "{place}/intruder"}}])
rel_x = f"skills/{n0}/{intruder['content_hash']}.json"
push_change(bare, BT / "merge" / "x", rel_x, intruder)
r = record_push(bare, BT / "rec" / "x", rel_x, "mallory", "999", "--report", report)
check(r.returncode == 2 and "REFUSED" in r.stderr and "retrying" not in r.stderr and "not written" in report.read_text()
      and not (BT / "rec" / "x" / "reviews" / n0 / f"{intruder['content_hash']}.json").exists(),
      f"record_push refuses another author's name for good: {r.stderr}")

# Out of attempts: loud, exit 1, and the report carries the exact recovery command, no credentials.
rejecting = BT / "rejecting.git"
subprocess.run(["git", "clone", "--quiet", "--bare", str(bare), str(rejecting)], check=True, timeout=60)
write(rejecting / "hooks" / "pre-receive", "#!/bin/sh\nexit 1\n").chmod(0o755)
report.unlink()
r = record_push(rejecting, BT / "rec" / "stranded", rel_x, a0, "998", "--report", report,
                "--attempts", "3", "--base-delay", "0.01", "--max-delay", "0.02")
text = report.read_text() if report.exists() else ""
check(r.returncode == 1 and "STRANDED" in r.stderr and "tools/record_push.py" in text and f"--path {rel_x}" in text
      and "--verdict auto_approved" in text and f"--author {a0}" in text and "--pr 998" in text,
      f"record_push gives up loudly with a recovery command: {r.stderr}{text}")
# Cancelled mid-retry (the job killed while record_push waits for main): the report is
# already there, with the recovery command, so the workflow's cancelled() step can post it.
report.unlink()
proc = subprocess.Popen([sys.executable, str(TOOLS / "record_push.py"), "--remote", str(rejecting), "--work",
                         str(BT / "rec" / "killed"), "--path", rel_x, "--verdict", "auto_approved", "--author", a0,
                         "--pr", "997", "--report", str(report), "--base-delay", "0.5", "--max-delay", "1"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
for _ in range(400):  # until it has cloned and is retrying against the rejecting remote
    if proc.poll() is not None or (report.exists() and (BT / "rec" / "killed" / ".git").is_dir()):
        break
    time.sleep(0.05)
time.sleep(0.3)
check(proc.poll() is None, "record_push is still retrying when it is killed")
proc.kill()
proc.wait(timeout=30)
text = report.read_text() if report.exists() else ""
check("stopped" in text and f"--path {rel_x}" in text and "--pr 997" in text,
      f"a record_push killed mid-retry leaves the report with the recovery command: {text!r}")

# yank_push on its own: unknown version refused for good; already-yanked is success with no new commit.
r = yank_push(bare, BT / "yank" / "missing", "yank_target", "b" * 64)
check(r.returncode == 2 and "REFUSED" in r.stderr and "retrying" not in r.stderr, f"yank of an unpublished version refused: {r.stderr}")
r = yank_push(bare, BT / "yank" / "bad", "../etc", "b" * 64)
check(r.returncode == 2, "yank_push refuses a path-like name")
head = g(bare, "rev-parse", "main").stdout.strip()
r = yank_push(bare, BT / "yank" / "again", "yank_target", YANK_REL_D["content_hash"])
check(r.returncode == 0 and "already yanked" in r.stdout and g(bare, "rev-parse", "main").stdout.strip() == head,
      f"re-running a yank is a no-op success: {r.stdout}{r.stderr}")

sys.path.insert(0, str(TOOLS))
import record_push as _rp  # noqa: E402

check(_rp.display_remote("https://x-access-token:SECRET@github.com/o/r.git") == "https://github.com/o/r.git",
      "the recovery command never shows the token from the workflow's remote URL")

# commit_index: an index built before an input change steps aside; one built before an
# unrelated change is committed on top of it; the same index twice is a no-op.
def build_and_sign(dest):
    clone = fresh_clone(bare, dest)
    tool("build_index.py", "--repo", clone, "--leutheria", REPO, "--refresh")
    tool("sign_index.py", "--index", clone / "index.json", "--out", clone / "index.json.sig", "--key-file", key_a)
    return clone, g(clone, "rev-parse", "HEAD").stdout.strip()


def commit_index(clone, built):
    return tool("commit_index.py", "--repo", clone, "--built-sha", built, "--index", clone / "index.json",
                "--sig", clone / "index.json.sig", "--base-delay", "0.01", "--max-delay", "0.02")


stale, stale_sha = build_and_sign(BT / "ci_stale")
push_change(bare, BT / "merge" / "y", "yanked.json", '{"yanked": []}\n')  # an input changes after that build
before = g(bare, "rev-parse", "main").stdout.strip()
r = commit_index(stale, stale_sha)
check(r.returncode == 0 and "superseded" in r.stdout and g(bare, "rev-parse", "main").stdout.strip() == before,
      f"commit_index steps aside when main's inputs moved after the build: {r.stdout}{r.stderr}")
cur, cur_sha = build_and_sign(BT / "ci_cur")
push_change(bare, BT / "merge" / "z", "README.md", "unrelated edit\n")  # not an input
r = commit_index(cur, cur_sha)
landed = fresh_clone(bare, BT / "ci_check")
check(r.returncode == 0 and "committed" in r.stdout and (landed / "README.md").read_text() == "unrelated edit\n"
      and (landed / "index.json").read_bytes() == (cur / "index.json").read_bytes(),
      f"commit_index lands the index on top of an unrelated change: {r.stdout}{r.stderr}")
r = commit_index(cur, cur_sha)
check(r.returncode == 0 and "unchanged" in r.stdout, f"committing the same index again is a no-op: {r.stdout}")
shutil.rmtree(BT, ignore_errors=True)


def tampered(mutate, label):
    copy = TMP / "tamper"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(repo, copy)
    mutate(copy)
    r = tool("verify_index.py", "--repo", copy, "--keys", keys, "--leutheria", REPO, "--now", NOW)
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
r = tool("verify_index.py", "--repo", repo, "--keys", other_keys, "--now", NOW)
check(r.returncode == 1 and "unknown signing key" in r.stderr, "client with a different key refuses the index")

# M3: a signed expiry, so a frozen copy of an old index stops verifying.
idx = index()
_ts = lambda s: datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
check(_ts(idx["expires_at"]) - _ts(idx["generated_at"]) == datetime.timedelta(days=7),
      f"expires_at is generated_at + 7 days: {idx['generated_at']} -> {idx['expires_at']}")
r = tool("verify_index.py", "--repo", repo, "--keys", keys, "--leutheria", REPO, "--now", "2026-10-23T00:00:00Z")
check(r.returncode == 1 and "expired" in r.stderr, f"an index past its expires_at fails verification: {r.stderr}")


def resigned(mutate_index):
    def go(c):
        d = json.loads((c / "index.json").read_text())
        mutate_index(d)
        (c / "index.json").write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
        tool("sign_index.py", "--index", c / "index.json", "--out", c / "index.json.sig", "--key-file", key_a)
    return go


tampered(resigned(lambda d: d.pop("expires_at")), "a validly signed index with no expires_at")
tampered(resigned(lambda d: d.update(expires_at="2027-10-15T10:00:00Z")), "a validly signed index valid for a year")
tampered(resigned(lambda d: d.update(expires_at=d["generated_at"])), "expires_at not after generated_at")
tampered(resigned(lambda d: d.update(expires_at=d["expires_at"].replace("Z", "+00:00"))), "expires_at not in the Z form")
before = (repo / "index.json").read_bytes()
r = tool("build_index.py", "--repo", repo, "--leutheria", REPO, "--now", "2026-10-17T09:00:00Z")
check("unchanged" in r.stdout and (repo / "index.json").read_bytes() == before, "a plain rebuild keeps the old timestamps")
r = tool("build_index.py", "--repo", repo, "--leutheria", REPO, "--now", "2026-10-17T09:00:00Z", "--refresh")
fresh = json.loads((repo / "index.json").read_text())
check(r.returncode == 0 and (fresh["generated_at"], fresh["expires_at"]) == ("2026-10-17T09:00:00Z", "2026-10-24T09:00:00Z"),
      "--refresh (the daily re-sign) moves generated_at and expires_at forward")
legacy = {k: v for k, v in fresh.items() if k != "expires_at"}
(repo / "index.json").write_bytes(json.dumps(legacy).encode())
r = tool("build_index.py", "--repo", repo, "--leutheria", REPO, "--now", "2026-10-17T10:00:00Z")
check("unchanged" not in r.stdout and json.loads((repo / "index.json").read_text()).get("expires_at"),
      "an index without expires_at is always rewritten")
tool("sign_index.py", "--index", repo / "index.json", "--out", repo / "index.json.sig", "--key-file", key_a)
check(tool("verify_index.py", "--repo", repo, "--keys", keys, "--leutheria", REPO, "--now", "2026-10-18T00:00:00Z")
      .returncode == 0, "the refreshed index re-signs and verifies")

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
sub_jobs = parsed["submission.yml"]["jobs"]["screen"]
first = sub_jobs["steps"][0]
check(first.get("if") == "github.event.action == 'synchronize'" and "--remove-label" in first["run"]
      and "human-approved-override" in first["run"], "a new push removes both approval labels before anything else")
check("human_approval.py verify" in sub and "--head-sha \"$HEAD_SHA\"" in sub and "HUMAN_OK" in sub
      and "github.event.action == 'labeled' && 'yes'" not in sub,
      "the label only triggers a check; approval comes from a review bound to the head sha")
check(sub.count("--override-static-reject") == 1 and "LABEL\" = human-approved-override" in sub,
      "the static-reject override needs its own label")
check(sub.index("--check-owner-only") < sub.index("gh pr merge"), "name ownership is re-checked against main before merging")
# Burst safety: the record step retries through record_push.py (not a 3-try shell loop, not a
# concurrency group, which would cancel queued runs), and a run that can't record says so.
check(sub.index("gh pr merge") < sub.index("tools/record_push.py") and "for attempt in" not in sub
      and "registry-unrecorded" in sub and "unrecorded-review-" in sub,
      "the review record is written by record_push.py after the merge, and a failure is labelled and kept")
check(all("concurrency" not in j for j in parsed["submission.yml"]["jobs"].values()),
      "no submission job joins a shared concurrency group")
group = json.dumps(parsed["submission.yml"]["concurrency"])
check("-solo-" in group and "pull_request.state == 'closed'" in group and "human-approved-override" in group,
      "an ignored label, or any event once the PR is merged, can't cancel a run that is merging or recording")
check("pull_request.state != 'closed'" in sub_jobs["if"], "a closed or merged PR is never screened again")
_steps = {st.get("name"): st for st in sub_jobs["steps"]}
_rep = _steps.get("Report a merge without a review record", {})
_keep = _steps.get("Keep what recovery needs", {})
check("cancelled()" in _rep.get("if", "") and "record_report.md" in _rep.get("if", "")
      and "registry-unrecorded" in _rep.get("run", "") and "--json state" in _rep.get("run", "")
      and "cancelled()" in _keep.get("if", ""),
      "a merge left unrecorded is reported even when the run is cancelled, and only if the PR really merged")
_act = _steps["Act on the decision"]["run"]
check(_act.index("> record_report.md") < _act.index("gh pr merge") < _act.index("tools/record_push.py")
      and "--report record_report.md" in _act,
      "the unrecorded-merge report exists from just before the merge until record_push confirms the record")
_yank = json.dumps(parsed["yank.yml"]["jobs"]["yank"])
check("tools/yank_push.py" in _yank and "git push" not in _yank and "tools/yank.py" not in _yank,
      "the yank is pushed only through yank_push.py (fresh main, fast-forward, backoff)")

# M5: the signing key meets as little code as possible.
import re  # noqa: E402

for w in workflows:
    for m in re.finditer(r"uses:\s*(\S+)", w.read_text()):
        ref = m.group(1)
        check(ref.startswith("./") or re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref),
              f"{w.name}: action pinned to a full commit sha: {ref}")
pub = parsed["publish.yml"]
jobs = pub["jobs"]
holders = [n for n, j in jobs.items() if "REGISTRY_SIGNING_KEY" in json.dumps(j)]
check(holders == ["sign"], f"only the sign job sees the signing key: {holders}")
for name in ("submission.yml", "yank.yml"):
    check("REGISTRY_SIGNING_KEY" not in json.dumps(parsed[name]), f"{name} never names the signing key")
sign_job = jobs["sign"]
sign_runs = " ".join(s.get("run", "") for s in sign_job["steps"])
check(sign_job.get("environment") == "registry-signing" and sign_job.get("permissions") == {"contents": "read"},
      "sign job runs in the protected registry-signing environment with read-only contents")
check("--require-hashes" in sign_runs and "requirements-sign.txt" in sign_runs and "requirements-ci" not in sign_runs,
      "sign job installs only the hash-pinned signing requirements")
check(set(sign_job["steps"][0]["with"]["sparse-checkout"].split()) ==
      {"tools/sign_index.py", "tools/common.py", "requirements-sign.txt"},
      "sign job checks out only the two scripts it runs and its requirements")
check(pub.get("permissions") == {"contents": "read"} and jobs["commit"]["permissions"] == {"contents": "write"}
      and "permissions" not in jobs["build"], "only the commit job can write to the repository")
check(not any("pip install" in s.get("run", "") for s in jobs["build"]["steps"]), "the build job installs nothing")
on = pub.get("on") or pub.get(True) or pub.get("true") or {}  # YAML 1.1 reads the key `on` as true
check("schedule" in on and "--refresh" in json.dumps(jobs["build"]), "publish re-signs daily with --refresh (M3)")
# Burst safety for publish: one workflow-level group (no per-job groups a queued commit job
# could be cancelled through), the commit goes through commit_index.py, and the push paths
# that start a publish are exactly the inputs commit_index.py steps aside for.
check(pub.get("concurrency") == {"group": "registry-publish", "cancel-in-progress": False}
      and all("concurrency" not in j for j in jobs.values()),
      "publish is serialised as a whole run, never per job")
check("tools/commit_index.py" in json.dumps(jobs["commit"]) and "git push" not in json.dumps(jobs["commit"]),
      "the index is committed only through commit_index.py")
sys.path.insert(0, str(TOOLS))
import commit_index as _ci  # noqa: E402

check(sorted(p.rstrip("/*") for p in on["push"]["paths"]) == sorted(_ci.INPUTS),
      f"publish's push paths are commit_index.INPUTS: {on['push']['paths']}")
check("registry-main-writes" not in json.dumps(parsed), "no workflow still uses the old shared group")
_req_text = (REGISTRY / "requirements-sign.txt").read_text()
# One requirement per logical line: pip-compile writes `name==ver \` then one `--hash=` per line.
reqs = [l.strip() for l in re.sub(r"[ \t]*\\\n\s*", " ", _req_text).splitlines() if l.strip() and not l.lstrip().startswith("#")]
check({l.split("==")[0] for l in reqs} == {"cryptography", "cffi", "pycparser"}, f"signing requirements are cryptography + its deps: {[l[:40] for l in reqs]}")
check(all(re.match(r"[\w-]+==[\w.]+ ", l) for l in reqs), "each signing requirement is pinned to one version")
if not all("--hash=sha256:" in l for l in reqs):
    check("TODO(hashes)" in _req_text, "missing hashes are marked TODO(hashes)")
    print("WARN  requirements-sign.txt has no hashes yet (TODO(hashes)): publish fails closed until they are generated")
else:
    check(all(re.fullmatch(r"[\w-]+==[\w.]+( --hash=sha256:[0-9a-f]{64})+", l) for l in reqs)
          and "TODO(hashes)" not in _req_text, "every signing requirement carries only well-formed sha256 hashes")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")
