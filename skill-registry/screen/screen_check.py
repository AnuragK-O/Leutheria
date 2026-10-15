"""Offline checks for the screener's decision logic and its handling of the
model's output. No network, no API key, no model call.

Deliberately uses only harmless skills: the cases that need a "blocked by
static analysis" result get it by substituting the analyzer's output, so this
file never has to contain an attack. What it proves is the wiring -- that a
static block can't be loosened by anything the model says, that an unusable
model never approves, and that approval needs every condition at once.

    python skill-registry/screen/screen_check.py
"""

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import screen  # noqa: E402
import static  # noqa: E402

CATALOG = json.loads((HERE.parent.parent / "agent" / "tool_catalog.json").read_text())
FAILED = []


def check(ok, name):
    print(("ok    " if ok else "FAIL  ") + name)
    if not ok:
        FAILED.append(name)


BENIGN = {
    "name": "list_downloads",
    "description": "List the files in the Downloads folder.",
    "input_schema": {"type": "object", "properties": {}, "required": []},
    "steps": [{"tool": "list_files", "args": {"path": "~/Downloads"}}],
}


def write_skill(skill) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="screen_check_"))
    path = tmp / "skill.json"
    path.write_text(json.dumps(skill))
    return path


class StubClient:
    """Stands in for anthropic.Anthropic: returns one canned response."""

    def __init__(self, text=None, stop_reason="end_turn", raise_exc=None):
        self.text, self.stop_reason, self.raise_exc = text, stop_reason, raise_exc
        self.requests = []
        self.messages = self

    def create(self, **request):
        self.requests.append(request)
        if self.raise_exc:
            raise self.raise_exc

        class Block:
            type = "text"

        block = Block()
        block.text = self.text

        class Usage:
            input_tokens, output_tokens = 100, 50
            cache_read_input_tokens = cache_creation_input_tokens = 0

        class Response:
            content = [block]
            usage = Usage()

        Response.stop_reason = self.stop_reason
        return Response()


def model_says(verdict, risk="low", matches=True):
    return json.dumps({
        "verdict": verdict, "risk": risk, "description_matches_steps": matches,
        "findings": [], "summary": "stub",
    })


def run(skill, client, force_static=None):
    """screen.screen() with an optional forced static verdict."""
    real = static.analyze
    if force_static:
        def fake(sk, tools):
            res = real(sk, tools)
            res["max_severity"] = force_static
            res["findings"] = res["findings"] + [{
                "where": "steps[0]", "rule": "forced_by_check", "severity": force_static,
                "issue": "forced by screen_check", "source": "static",
            }]
            return res
        static.analyze = fake
    try:
        result, request = screen.screen(write_skill(skill), CATALOG, client=client)
    finally:
        static.analyze = real
    return result, request


def main():
    # Benign + approving model -> approve.
    client = StubClient(model_says("approve"))
    result, _ = run(BENIGN, client)
    check(result["verdict"] == "approve" and result["static"]["verdict"] == "clean",
          "benign skill, model approves -> approve")
    check(len(client.requests) == 1, "the model is called exactly once")

    # A static block can't be loosened, and the model isn't even asked.
    client = StubClient(model_says("approve"))
    result, request = run(BENIGN, client, force_static=static.BLOCK)
    check(result["verdict"] == "reject" and result["risk"] == "high", "static block -> reject at high risk")
    check(not client.requests and request is None, "static block -> model never called")

    # needs_review in static: an approving model still only gets a flag.
    result, _ = run(BENIGN, StubClient(model_says("approve")), force_static=static.REVIEW)
    check(result["verdict"] == "flag" and result["risk"] in ("medium", "high"),
          "static needs_review + model approve -> flag, risk >= medium")

    # Each approve-blocking condition on its own.
    result, _ = run(BENIGN, StubClient(model_says("approve", matches=False)))
    check(result["verdict"] == "flag", "model approve but description mismatch -> flag")
    result, _ = run(BENIGN, StubClient(model_says("approve", risk="high")))
    check(result["verdict"] == "flag", "model approve at high risk -> flag")

    # The model can tighten.
    result, _ = run(BENIGN, StubClient(model_says("reject", risk="high")))
    check(result["verdict"] == "reject", "model reject -> reject")
    result, _ = run(BENIGN, StubClient(model_says("flag", risk="medium")))
    check(result["verdict"] == "flag", "model flag -> flag")

    # An unusable model never approves.
    for label, client in [
        ("refusal", StubClient(model_says("approve"), stop_reason="refusal")),
        ("truncated output", StubClient(model_says("approve"), stop_reason="max_tokens")),
        ("malformed JSON", StubClient("not json at all")),
        ("off-schema JSON", StubClient(json.dumps({"verdict": "yes please"}))),
        ("API error", StubClient(raise_exc=RuntimeError("billing: credit balance too low"))),
    ]:
        result, _ = run(BENIGN, client)
        check(result["verdict"] == "flag", f"{label} -> flag, never approve")

    # No key and no client -> flag, model not called.
    import os
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        result, _ = screen.screen(write_skill(BENIGN), CATALOG, client=None)
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved
    check(result["verdict"] == "flag" and result["model"]["status"] == "no_key", "no API key -> flag")

    # The skill reaches the model only inside the data block, as escaped JSON;
    # nothing from it is placed in the system prompt.
    marker = "MARKER-7f3a"
    skill = dict(BENIGN, description=f"List the Downloads folder. {marker}")
    client = StubClient(model_says("approve"))
    run(skill, client)
    request = client.requests[0]
    system_text = json.dumps(request.get("system", ""))
    check(marker not in system_text, "skill text never appears in the system prompt")
    check(marker in json.dumps(request["messages"]), "skill text is present in the message data block")

    # run_command auto-publish allowlist (review M4). Benign commands only: what is proved is
    # the shape rule -- listed program, listed subcommand and options, no shell syntax, no
    # parameter -- not that any particular attack is caught.
    tools = static.load_catalog_tools(CATALOG)

    def cmd_skill(cmd, params=None, description="Show some information about this Mac."):
        return {"name": "show_info", "description": description,
                "input_schema": {"type": "object", "properties": params or {}, "required": []},
                "steps": [{"tool": "run_command", "args": {"cmd": cmd}}]}

    def rules(cmd, params=None):
        return {f["rule"] for f in static.analyze(cmd_skill(cmd, params), tools)["findings"]}

    for cmd in ["git status -sb", "git log --oneline -n 5", "git log --since=2.weeks --stat", "git diff --stat",
                "git branch --show-current", "brew info wget", "brew list --versions", "brew outdated", "brew update",
                "brew --version", "ls -la ~/Documents", "date +%Y-%m-%d", "date -u", "echo hello there", "uname -a",
                "sw_vers -productVersion", "uptime", "df -h", "du -sh ~/Downloads"]:
        check("not_allowlisted" not in rules(cmd), f"allowlisted: {cmd}")
    for label, cmd, params in [
        ("program not listed", "python3 --version", None),
        ("program given as a path", "/bin/ls -la", None),
        ("git subcommand not on the read-only list", "git config --list", None),
        ("git global option", "git -C ~/Projects status", None),
        ("git option not on the subcommand's list", "git log --output=notes.txt", None),
        ("git operand where none is allowed", "git branch newname", None),
        ("brew subcommand not on the list", "brew install wget", None),
        ("brew tap-qualified name", "brew info someone/sometap/thing", None),
        ("two commands chained", "ls ~/Documents | wc -l", None),
        ("quoting", "echo 'hello'", None),
        ("date operand", "date 0101", None),
        ("environment assignment prefix", "LANG=C ls", None),
        ("a parameter in the command", "ls {folder}", {"folder": {"type": "string", "description": "Folder."}}),
        ("too long", "echo " + "a " * 120, None),
    ]:
        check("not_allowlisted" in rules(cmd, params), f"not allowlisted -> needs_review: {label}")
    said = "Run the git status shell command to show the state of the current repository."
    result, _ = run(cmd_skill("git config --list", description=said), StubClient(model_says("approve")))
    check(result["static"]["verdict"] == "needs_review" and result["verdict"] == "flag"
          and {f["rule"] for f in result["static"]["findings"]} == {"not_allowlisted"},
          "an unlisted command, otherwise clean, with an approving model is flagged for a person")
    result, _ = run(cmd_skill("git status -sb", description=said), StubClient(model_says("approve")))
    check(result["static"]["verdict"] == "clean" and result["verdict"] == "approve",
          "an allowlisted command with an approving model can still auto-approve")

    # Output contract decide.py relies on.
    result, _ = run(BENIGN, StubClient(model_says("approve")))
    keys = {"verdict", "risk", "findings", "summary", "description_matches_steps", "static", "model", "screen_version"}
    check(keys <= set(result) and result["static"]["verdict"] in ("clean", "needs_review", "reject"),
          "result carries the fields decide.py reads")

    print("\n" + ("ALL CHECKS PASSED" if not FAILED else f"{len(FAILED)} FAILED"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
