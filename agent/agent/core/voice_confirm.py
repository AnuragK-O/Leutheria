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


# --- which approvals a spoken "yes" may give -----------------------------------
#
# A voice-only user hears the prompt; they don't see the command. So a spoken
# yes is accepted only for a command the prompt read out in full and that a
# listener can actually judge from hearing it (review H4). Anything else needs
# a click on the card; a spoken "no" is always accepted.

# Longest command read aloud in full. The prompt reads exactly this much, so
# a command longer than this was not heard in full and can't be voice-approved.
SPOKEN_COMMAND_MAX = 200

# Shell syntax that chains, substitutes, redirects or expands: hearing
# "echo hi; curl ..." read aloud does not make it clear what runs.
_SHELL_META = ("|", ";", "&", "`", "$", ">", "<", "\n", "\r", "\\", "(", ")", "{", "}", "*", "?", "~", "!", "#", "=")

# Programs that fetch, run other code, or elevate. Any appearance as a word
# needs a click, wherever it is in the command.
_RISKY_PROGRAMS = frozenset(
    {
        "curl", "wget", "nc", "ncat", "netcat", "socat", "telnet", "ssh", "scp", "sftp", "rsync", "ftp",
        "bash", "sh", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "env", "exec", "eval", "source", "xargs",
        "python", "python3", "python2", "perl", "ruby", "node", "deno", "bun", "php", "lua", "osascript",
        "swift", "awk", "gawk", "sed", "find", "open", "sudo", "su", "doas", "launchctl", "defaults",
        "crontab", "base64", "xxd", "openssl", "security", "dd", "chmod", "chown", "rm", "mv", "git",
        "npm", "npx", "pip", "pip3", "brew", "make", "docker", "kubectl", "tee", "nohup", "screen", "tmux",
    }
)


def needs_click(cmd) -> bool:
    """True if a spoken "yes" must NOT approve running this shell command.

    False only for a short, plain command: at most SPOKEN_COMMAND_MAX
    characters (so it was read out whole), printable ASCII only (no
    look-alike letters or hidden characters), none of the shell's
    chaining / substitution / redirection / globbing characters, and no
    word naming a downloader, interpreter, privilege tool or deleter.
    When in doubt it returns True: the cost is one click.
    """
    if not isinstance(cmd, str):
        return True
    text = cmd.strip()
    if not text or len(text) > SPOKEN_COMMAND_MAX:
        return True
    if any(not (32 <= ord(ch) < 127) for ch in text):
        return True
    if any(meta in text for meta in _SHELL_META):
        return True
    if "'" in text or '"' in text:
        return True  # quoting changes what words are; reading it aloud loses that
    for word in text.split():
        name = word.rsplit("/", 1)[-1].lower()
        if name in _RISKY_PROGRAMS or name.startswith(("python", "pip", "ruby", "perl", "node")):
            return True
    return False


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
