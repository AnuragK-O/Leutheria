"""Shared plumbing for the system/productivity skills.

Every step goes through dispatch() -- never a tool function called directly --
so each one is subject to the Library's enable/ask-first switches and the
safety tiers exactly as if the model had called it (CLAUDE.md). A failed step
stops the skill there: the result names the step, carries its error, and
lists what had already been done, because some of these skills change things
(volume, timers) before the step that fails.
"""


class StepFailed(Exception):
    def __init__(self, tool: str, error: str):
        super().__init__(error)
        self.tool = tool
        self.error = error


class Steps:
    def __init__(self, websocket, pending):
        self.websocket = websocket
        self.pending = pending
        self.steps = []

    async def run(self, tool: str, args: dict = None) -> dict:
        from agent.core.dispatcher import dispatch  # lazy: dispatcher imports the skills registry

        args = args or {}
        result = await dispatch(tool, args, self.websocket, self.pending)
        self.steps.append({"tool": tool, "args": args, "result": result})
        if not isinstance(result, dict) or not result.get("ok"):
            error = result.get("error") if isinstance(result, dict) else None
            raise StepFailed(tool, error or "it did not succeed")
        return result

    def failed(self, failure: StepFailed) -> dict:
        done = [s["tool"] for s in self.steps[:-1]]
        error = f"The {failure.tool} step failed: {failure.error}"
        if done:
            error += f" Steps completed before it: {', '.join(done)}."
        return {"ok": False, "error": error, "failed_step": failure.tool, "steps": self.steps}

    def done(self, message: str, **extra) -> dict:
        return {"ok": True, "message": message, **extra, "steps": self.steps}


def invalid(error: str) -> dict:
    """A bad argument, caught before any step runs."""
    return {"ok": False, "error": error, "steps": []}


def number(args: dict, name: str, default, low: float, high: float):
    """args[name] as a number within [low, high], or the default when absent.
    Raises ValueError with a sentence the model can relay."""
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError(f"{name} must be a number")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}")
    return value


def battery_phrase(battery: dict, low: int = 20) -> tuple:
    """(sentence, needs_power) from a get_battery_status result."""
    if not battery.get("has_battery"):
        return "This Mac runs on mains power.", False
    percent = battery.get("percent")
    on_battery = str(battery.get("state", "")).strip().lower() == "discharging"
    if not on_battery:
        return f"Battery is at {percent} percent and plugged in.", False
    if percent is not None and percent <= low:
        return f"Battery is low at {percent} percent and not charging, so plug in soon.", True
    return f"Battery is at {percent} percent.", False
