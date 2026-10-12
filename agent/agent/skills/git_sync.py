from agent.skills import dev_common as dc

ACTIONS = {
    # --ff-only: a pull that would need a merge or rebase stops and says so
    # instead of leaving the repository half-way through one.
    "pull": ("pull --ff-only", "Pulled the latest changes"),
    "push": ("push", "Pushed"),
}


async def run(args: dict, websocket, pending: dict) -> dict:
    """Pull or push a repository's current branch. git_status confirms it is
    a repository (read-only); the network operation is a run_command, so it
    is shown and approved first."""
    steps = dc.Steps(websocket, pending)
    path = dc.text_arg(args, "path")
    action = dc.text_arg(args, "action").lower()
    if not path:
        return steps.fail("I need the folder of the repository.")
    if action not in ACTIONS:
        return steps.fail("The action must be pull or push.")

    status = await steps.run("git_status", {"path": path, "log_count": 0})
    if not status.get("ok"):
        return steps.step_failed("checking the repository", status)
    if not status.get("remote_url"):
        return steps.fail("This repository has no remote called origin, so there is nothing to sync with.")
    if status.get("detached"):
        return steps.fail("The repository is on a detached HEAD, not a branch, so I am not syncing it.")
    if action == "push" and status.get("upstream") and not status.get("ahead"):
        return steps.done(f"Nothing to push: {status['branch']} is not ahead of {status['upstream']}.")

    git_args, done = ACTIONS[action]
    root = dc.q(status.get("root") or dc.expand(path))
    result = await steps.run("run_command", {"cmd": f"git -C {root} {git_args}"})
    if not result.get("ok"):
        return steps.step_failed(f"git {action}", result)
    return steps.done(f"{done} on {status.get('branch')}.")
