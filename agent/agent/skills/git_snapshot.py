import fnmatch
from pathlib import PurePosixPath

from agent.skills import dev_common as dc

MAX_MESSAGE = 500

# `git add -A` stages everything, including the untracked file nobody meant
# to commit. These are the names where that mistake is expensive.
SENSITIVE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "id_dsa", "id_ecdsa",
    "id_ed25519", "*.keystore", "credentials.json", "secrets.*", ".npmrc", ".netrc",
)
SENSITIVE_OK = (".env.example", ".env.sample", ".env.template")


def sensitive_files(files: list) -> list:
    hits = []
    for entry in files:
        if "D" in entry.get("status", ""):
            continue  # a deletion can't leak anything
        name = PurePosixPath(entry.get("path", "").strip('"').rstrip("/")).name.lower()
        if name in SENSITIVE_OK:
            continue
        if any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_PATTERNS):
            hits.append(entry["path"])
    return hits


async def run(args: dict, websocket, pending: dict) -> dict:
    """Stage everything in a repository and commit it with a message.

    git_status first (read-only): it proves this is a repository, stops early
    when there is nothing to commit, and lets the skill refuse to sweep up a
    file that looks like a secret. The commit itself is one run_command, so
    the user sees and approves the exact command.
    """
    steps = dc.Steps(websocket, pending)
    path = dc.text_arg(args, "path")
    message = dc.text_arg(args, "message")
    if not path:
        return steps.fail("I need the folder of the repository to commit.")
    if not message:
        return steps.fail("I need a commit message.")
    if len(message) > MAX_MESSAGE:
        return steps.fail(f"That commit message is longer than {MAX_MESSAGE} characters.")

    status = await steps.run("git_status", {"path": path, "log_count": 1})
    if not status.get("ok"):
        return steps.step_failed("checking the repository", status)
    if status.get("clean"):
        return steps.done("Nothing to commit: the working tree is clean.", committed=False)
    if status.get("conflicted"):
        return steps.fail(
            f"{status['conflicted']} files have merge conflicts. Resolve those before committing."
        )

    risky = sensitive_files(status.get("files", []))
    if risky and not dc.flag(args, "allow_sensitive"):
        shown = ", ".join(risky[:5])
        return steps.fail(
            f"Not committing: {shown} looks like it holds secrets and would be included. "
            "Add it to the gitignore, or ask again and say to include it anyway.",
            sensitive_files=risky[:20],
        )
    if status.get("files_truncated") and not dc.flag(args, "allow_sensitive"):
        return steps.fail(
            f"{status['changed_files']} files have changed, more than I can check for secrets. "
            "Ask again and say to include everything if that is what you want."
        )

    root = dc.q(status.get("root") or dc.expand(path))
    cmd = f"git -C {root} add -A && git -C {root} commit -m {dc.q(message)}"
    result = await steps.run("run_command", {"cmd": cmd})
    if not result.get("ok"):
        return steps.step_failed("the commit", result, committed=False)

    count = status.get("changed_files", 0)
    branch = status.get("branch") or "a detached HEAD"
    plural = "file" if count == 1 else "files"
    return steps.done(f"Committed {count} changed {plural} on {branch}: {message}", committed=True)
