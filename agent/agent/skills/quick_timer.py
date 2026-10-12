import re

from agent.skills.system_steps import Steps, StepFailed, invalid

UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
}
WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "half": 0.5,
}
# Lookarounds rather than \b: "1h30m" has no word boundary between h and 3
# (so digits may follow a letter), a number word may not ("often minutes"),
# and "10 months" must not read as ten minutes.
_AMOUNT = r"((?<![0-9.])\d+(?:\.\d+)?|(?<![a-z0-9.-])(?:" + "|".join(sorted(WORDS, key=len, reverse=True)) + r"))"
_UNIT = "(" + "|".join(sorted(UNITS, key=len, reverse=True)) + r")(?![a-z])"
_PAIR = re.compile(_AMOUNT + r"\s*(?:an?\s+)?" + _UNIT)
_AND_A_HALF = re.compile(_AMOUNT + r"\s+and a half\s+" + _UNIT)  # "two and a half minutes"


def parse_duration(text: str):
    """Seconds for a spoken duration, or None if it can't be read with
    confidence. Understands '10 minutes', '1 hour 30 minutes', '90 seconds',
    '1h30m', 'an hour and a half', 'half an hour', 'two and a half minutes'
    and 'mm:ss'. Deliberately strict: anything left over that isn't filler
    makes the whole string unreadable, since a timer for the wrong length is
    worse than being asked again."""
    if not isinstance(text, str):
        return None
    text = " ".join(text.lower().replace(",", " ").split())
    if not text:
        return None

    clock = re.fullmatch(r"(\d{1,2}):([0-5]\d)(?::([0-5]\d))?", text)
    if clock:
        first, second, third = int(clock.group(1)), int(clock.group(2)), clock.group(3)
        return first * 3600 + second * 60 + int(third) if third else first * 60 + second

    total = 0.0
    last_unit = None
    matched = False

    def take(match, extra=0.0):
        nonlocal total, last_unit, matched
        amount = match.group(1)
        value = float(amount) if amount[0].isdigit() else WORDS[amount]
        last_unit = UNITS[match.group(2)]
        total += (value + extra) * last_unit
        matched = True
        return " "

    rest = _AND_A_HALF.sub(lambda m: take(m, 0.5), text)
    rest = _PAIR.sub(take, rest)
    # "an hour and a half" / "two and a half minutes": the half belongs to
    # the unit just named.
    if matched and re.search(r"\band a half\b", rest):
        total += 0.5 * last_unit
        rest = re.sub(r"\band a half\b", " ", rest)
    rest = re.sub(r"\b(and|for|of|timer|about)\b", " ", rest).strip()
    if not matched or rest or total <= 0:
        return None
    return total


async def run(args: dict, websocket, pending: dict) -> dict:
    """Start a timer from a duration given in words and put a banner on
    screen saying when it ends. Two steps: set_timer, show_notification."""
    seconds = parse_duration(args.get("duration"))
    if seconds is None:
        return invalid("I couldn't read that duration. Give it like 10 minutes, 90 seconds, or 1 hour 30 minutes.")
    label = args.get("label") or ""
    if not isinstance(label, str):
        return invalid("label must be text")

    steps = Steps(websocket, pending)
    try:
        timer_args = {"seconds": seconds}
        if label.strip():
            timer_args["label"] = label.strip()
        timer = await steps.run("set_timer", timer_args)
        await steps.run(
            "show_notification",
            {"title": "Timer started", "message": f"{timer['duration']}, ending at {timer['ends_at']}"},
        )
    except StepFailed as failure:
        return steps.failed(failure)
    return steps.done(timer["message"], timer_id=timer["timer_id"], ends_at=timer["ends_at"])
