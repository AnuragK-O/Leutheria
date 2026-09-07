import re

# Word-boundary patterns, not plain substrings -- "note" must not match "no".
_YES_PATTERNS = [
    r"\byes\b", r"\byeah\b", r"\byep\b", r"\byup\b", r"\bsure\b", r"\bok(ay)?\b",
    r"\bapprove[d]?\b", r"\bconfirm(ed)?\b", r"\bdo it\b", r"\bgo ahead\b", r"\baffirmative\b",
]
_NO_PATTERNS = [
    r"\bno\b", r"\bnope\b", r"\bnah\b", r"\bdon'?t\b", r"\bdo not\b", r"\bcancel\b",
    r"\bdecline[d]?\b", r"\bstop\b", r"\bnegative\b",
]


def interpret_yes_no(text: str):
    """Best-effort yes/no classification of a short spoken reply to a
    confirmation prompt. Returns True (approve), False (decline), or None
    if it's ambiguous (both or neither matched) or empty -- callers should
    treat None as "not an answer, don't guess" and fall back to handling it
    as a normal command instead.
    """
    lowered = text.lower().strip()
    if not lowered:
        return None

    is_yes = any(re.search(p, lowered) for p in _YES_PATTERNS)
    is_no = any(re.search(p, lowered) for p in _NO_PATTERNS)

    if is_yes and not is_no:
        return True
    if is_no and not is_yes:
        return False
    return None
