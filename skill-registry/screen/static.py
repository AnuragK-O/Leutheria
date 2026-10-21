"""Deterministic static analysis of a shared skill (registry spec, component B).

Runs before the model screener and cannot be argued with: what it blocks stays
blocked whatever the model says. It reads the skill as an attacker would write
it -- every string a step will receive (after placeholder substitution with
each declared default/enum value), the description and every piece of prose
the end-user's model will see.

Shell strings are judged on several "views" at once: as written, with quoting,
backslashes, ANSI-C/hex/octal escapes, $IFS, simple variable assignments and
brace expansion undone, and with embedded base64/hex blobs decoded and
analysed recursively. A computed program name (a variable, a command
substitution or a glob in command position) is itself a block: whatever the
trick, a shared skill has no reason to hide which program it runs, and the
installing user's confirmation card would show the trick, not the program.

Findings never quote the skill's text back: they carry a location ("where"),
a rule id and a fixed explanation, so attacker prose can't reach a PR comment
or a reviewer's terminal through this module.

Stdlib only, no macOS imports: CI runs this on Linux.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import fnmatch
import re
import unicodedata
from urllib.parse import urlsplit

ANALYZER_VERSION = 3  # 2: run_command auto-publish allowlist; 3: param_is_program exempt for a bare {param} text / display-only text

BLOCK = "block"
REVIEW = "needs_review"
INFO = "info"
_RANK = {INFO: 0, REVIEW: 1, BLOCK: 2}


def worst(severities) -> str | None:
    best = None
    for s in severities:
        if best is None or _RANK[s] > _RANK[best]:
            best = s
    return best


class _Findings:
    def __init__(self):
        self.items = []
        self._seen = set()

    def add(self, where: str, rule: str, severity: str, issue: str):
        key = (where, rule)
        if key in self._seen:
            # keep the strongest severity for a repeated (where, rule)
            for f in self.items:
                if (f["where"], f["rule"]) == key and _RANK[severity] > _RANK[f["severity"]]:
                    f["severity"] = severity
            return
        self._seen.add(key)
        self.items.append({"where": where, "rule": rule, "severity": severity, "issue": issue, "source": "static"})


# --- placeholders (same grammar as agent/skills/template_skill.py) ---------------
# Copied, not imported: this module must run in the registry's CI with no
# Leutheria checkout on the path. Keep in sync with template_skill.PARAM_PATTERN.
PARAM_PATTERN = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
TOKEN_RE = re.compile(r"\{\{|\}\}|\{(" + PARAM_PATTERN + r")\}")
PARAM_MARK = "LXPARAM"  # what an unknown param value looks like to the analyzer


def substitute(text: str, values: dict) -> str:
    """One-pass substitution, exactly as run_template does it. A param with
    no value in `values` becomes LXPARAM_<name> so its position is visible."""

    def tok(m):
        if m.group(1):
            name = m.group(1)
            return str(values[name]) if name in values else f"{PARAM_MARK}_{name}"
        return m.group(0)[0]

    return TOKEN_RE.sub(tok, text)


def param_variants(schema: dict) -> list:
    """Value assignments to try: all-unknown, then each default/enum value.
    Bounded so a 20-param, 50-enum skill can't blow up analysis time."""
    props = (schema or {}).get("properties") or {}
    variants = [{}]
    defaults = {n: s["default"] for n, s in props.items() if isinstance(s, dict) and "default" in s}
    if defaults:
        variants.append(defaults)
    for n, s in props.items():
        if isinstance(s, dict) and isinstance(s.get("enum"), list):
            for v in s["enum"][:20]:
                variants.append({**defaults, n: v})
    return variants[:60]


# --- character-level tricks ---------------------------------------------------------

ZERO_WIDTH = set("​‌‍‎‏⁠⁡⁢⁣⁤﻿­͏᠎")
BIDI = set("‪‫‬‭‮⁦⁧⁨⁩؜")


def hidden_chars(text: str) -> tuple:
    """(severity, label) for the worst invisible character class present."""
    sev, label = None, None
    for ch in text:
        o = ord(ch)
        if ch in BIDI or 0xE0000 <= o <= 0xE007F:
            return BLOCK, "bidirectional-override or Unicode tag characters (text that displays differently from what it is)"
        if ch in ZERO_WIDTH or (o < 32 and ch not in "\n\t") or o == 0x7F or 0x80 <= o < 0xA0:
            sev, label = REVIEW, "zero-width or control characters"
        elif 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF:
            sev, label = REVIEW, "variation selectors (can carry hidden data)"
    return sev, label


def strip_hidden(text: str) -> str:
    return "".join(
        ch for ch in text
        if ch not in ZERO_WIDTH and ch not in BIDI and not (0xE0000 <= ord(ch) <= 0xE007F)
        and not (0xFE00 <= ord(ch) <= 0xFE0F) and not (0xE0100 <= ord(ch) <= 0xE01EF)
    )


# --- decoding embedded blobs -------------------------------------------------------

B64_RE = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
HEX_RE = re.compile(r"(?:\\?x?[0-9a-fA-F]{2}){10,}")


def _printable_text(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(text) < 6:
        return None
    ok = sum(1 for c in text if c.isprintable() or c in "\n\t")
    letters = sum(1 for c in text if c.isalpha())
    if ok / len(text) < 0.95 or letters / len(text) < 0.4:
        return None
    return text


def decoded_blobs(text: str) -> list:
    """Plain text hidden in base64 / hex runs inside `text`."""
    out = []
    for m in B64_RE.finditer(text):
        tok = m.group(0)
        for alt in (tok, tok.replace("-", "+").replace("_", "/")):
            pad = alt + "=" * (-len(alt) % 4)
            try:
                raw = base64.b64decode(pad, validate=True)
            except (binascii.Error, ValueError):
                continue
            t = _printable_text(raw)
            if t:
                out.append(t)
                break
    for m in HEX_RE.finditer(text):
        digits = re.sub(r"\\?x", "", m.group(0))
        if len(digits) % 2:
            digits = digits[:-1]
        try:
            t = _printable_text(bytes.fromhex(digits))
        except ValueError:
            t = None
        if t:
            out.append(t)
    return out


# --- shell normalisation -------------------------------------------------------------

def _decode_escapes(s: str) -> str:
    s = re.sub(r"\\[xX]([0-9a-fA-F]{1,2})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\U([0-9a-fA-F]{8})", lambda m: chr(min(int(m.group(1), 16), 0x10FFFF)), s)
    s = re.sub(r"\\0?([0-7]{3})", lambda m: chr(int(m.group(1), 8) & 0xFF), s)
    return s


def _brace_expand(s: str) -> str:
    """Bash brace expansion for simple `pre{a,b}post` words (bounded)."""
    for _ in range(4):
        m = re.search(r"(\S*?)\{([^{}\s]*,[^{}\s]*)\}(\S*)", s)
        if not m:
            break
        pre, body, post = m.groups()
        words = " ".join(pre + part + post for part in body.split(",")[:16])
        s = s[: m.start()] + words + s[m.end():]
    return s


_ASSIGN_RE = re.compile(
    r"(?:^|[;&|\s(`{])(?:export\s+|local\s+|readonly\s+|declare\s+(?:-\w+\s+)*|typeset\s+(?:-\w+\s+)*)?"
    r"([A-Za-z_][A-Za-z0-9_]*)=([^\s;&|()`]*)"
)
_READ_RE = re.compile(r"\bread\s+(?:-\w+\s+)*([A-Za-z_]\w*)\s*<<<\s*([^\s;&|]+)")


def _expand_vars(s: str) -> str:
    env = {}
    for m in _ASSIGN_RE.finditer(s):
        env[m.group(1)] = m.group(2)
    for m in _READ_RE.finditer(s):
        env[m.group(1)] = m.group(2)
    if not env:
        return s
    for _ in range(5):
        new = re.sub(r"\$\{([A-Za-z_]\w*)\}", lambda m: env.get(m.group(1), m.group(0)), s)
        new = re.sub(r"\$([A-Za-z_]\w*)", lambda m: env.get(m.group(1), m.group(0)), new)
        if new == s:
            break
        s = new
    return s


def shell_views(raw: str) -> dict:
    """Named normalisations of a shell string. Every rule runs on all of them."""
    s = strip_hidden(raw)
    lit = s.lower()
    esc = _decode_escapes(s)
    esc = re.sub(r"\\\n", "", esc)
    esc = re.sub(r"\\(.)", r"\1", esc)  # \c\u\r\l -> curl
    noq = esc.replace("'", "").replace('"', "")  # c''u""rl -> curl
    noq = re.sub(r"\$\{?IFS(?:[^}\s]*\})?", " ", noq)
    noq = noq.replace("\t", " ")
    deob = _brace_expand(_expand_vars(noq)).lower()
    # pretty-printing JSON is not "piping into an interpreter"
    json_tool = re.compile(r"\|\s*python[0-9.]*\s+-m\s+json\.tool\b")
    noq, deob = json_tool.sub("| jsonfmt", noq), json_tool.sub("| jsonfmt", deob)
    return {"literal": lit, "dequoted": noq.lower(), "deobfuscated": deob}


SEP_RE = re.compile(r"\|\||&&|[;|&\n(){}`]|\$\(|<\(|>\(")
WRAPPERS = {
    "sudo", "doas", "env", "exec", "command", "builtin", "nohup", "time", "nice", "xargs", "then", "do",
    "else", "if", "while", "until", "!", "caffeinate", "arch", "stdbuf", "timeout", "gtimeout", "unbuffer",
    "script", "watch", "eval", "source", ".",
}


def command_words(view: str) -> list:
    """The program word of each simple command (best effort, over-approximating)."""
    words = []
    for seg in SEP_RE.split(view):
        toks = seg.split()
        i = 0
        while i < len(toks):
            t = toks[i]
            if re.fullmatch(r"[a-z_][a-z0-9_]*=\S*", t) or t.startswith("-") or t in WRAPPERS or re.fullmatch(r"\d+", t):
                i += 1
                continue
            words.append(t)
            break
    return words


def tokens(view: str) -> list:
    return [t for t in re.split(r"[\s;|&()<>`{}]+|\$\(", view) if t]


def basename(tok: str) -> str:
    return tok.rstrip("/").rsplit("/", 1)[-1]


# --- vocabularies --------------------------------------------------------------------

INTERPRETERS = {
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "python", "python2", "python3", "perl", "ruby",
    "node", "nodejs", "deno", "bun", "php", "lua", "tclsh", "swift", "osascript", "jxa", "pwsh", "powershell",
    "awk", "gawk", "expect", "irb", "R", "rscript",
}
FETCHERS = {"curl", "wget", "aria2c", "lwp-download", "lwp-request", "ftp", "tftp", "sftp", "scp", "rsync", "ssh", "git-remote-http", "httpie", "xh"}
RAW_NET = {"nc", "ncat", "netcat", "socat", "telnet", "websocat", "wscat", "cryptcat"}
DNS_PING = {"dig", "nslookup", "host", "ping", "ping6", "traceroute", "drill"}
MAILERS = {"mail", "mailx", "sendmail", "mutt", "msmtp"}
PRIV = {"sudo", "su", "doas", "pkexec", "visudo", "dscl", "dseditgroup", "sysadminctl", "authorizationdb"}
DECODERS = {"base64", "base32", "xxd", "uudecode", "openssl", "rev", "gzip", "gunzip", "zcat", "bzcat", "xzcat", "basenc"}
DISK = {"diskutil", "mkfs", "newfs", "newfs_apfs", "newfs_hfs", "shred", "srm", "fdisk", "gpt", "asr", "wipefs"}
EVASION = {"spctl", "csrutil", "tccutil", "nvram", "fdesetup", "systemsetup", "kextload", "kmutil", "profiles", "bputil"}
PERSIST_PROGS = {"launchctl", "crontab", "at", "atq", "batch", "periodic", "sfltool", "emond"}
WRITER_PROGS = {"tee", "cp", "mv", "ln", "install", "ditto", "rsync", "touch", "mkdir", "plutil", "defaults", "echo", "printf", "cat", "sed", "perl", "dd", "curl", "wget", "unzip", "tar", "ed", "ex", "vi", "vim", "nano"}
SCRIPT_HOSTS = {"automator", "shortcuts", "open"}
# Everything worth seeing through a glob: `/???/??n/c?rl`.
GLOB_TARGETS = INTERPRETERS | FETCHERS | RAW_NET | PRIV | DECODERS | DISK | EVASION | PERSIST_PROGS | {"rm", "security", "eval", "xattr", "chmod", "launchctl", "osascript"}

SECRET_RE = re.compile(
    r"(?:\.ssh\b|\bid_(?:rsa|dsa|ecdsa|ed25519)\b|authorized_keys|known_hosts|\.gnupg|\.aws\b|\.azure\b|"
    r"\.config/gcloud|\.kube/config|\.docker/config|\.netrc|\.npmrc|\.pypirc|\.git-credentials|"
    r"(?:^|[/\s])\.env\b|\.pem\b|\.p12\b|\.key\b|keychains?\b|\bkeychain\b|login\.keychain|"
    r"cookies(?:\.binarycookies)?\b|login data|web data|\blocal state\b|1password|bitwarden|lastpass|"
    r"\.zsh_history|\.bash_history|\.psql_history|\.mysql_history|\.python_history|"
    r"wallet\.dat|\.electrum|metamask|exodus|\.bitcoin|tcc\.db|/etc/(?:passwd|shadow|master\.passwd|sudoers)|"
    r"anthropic_api_key|elevenlabs_api_key|leutheria_token)"
)
SECRET_ENV_RE = re.compile(r"\$\{?[a-z0-9_]*(?:key|token|secret|passw(?:or)?d|pass|credential|auth|cookie|session)[a-z0-9_]*\}?")
ENV_VAR_RE = re.compile(r"\$\{?(?!home\b|pwd\b|user\b|lxparam)[a-z_][a-z0-9_]*\}?")
PERSIST_PATH_RE = re.compile(
    r"(?:launchagents|launchdaemons|startupitems|loginitems|login items|backgroundtaskmanagement|loginhook|logouthook|"
    r"/etc/(?:periodic|rc\.|launchd|profile|zshrc|zprofile|bashrc|paths|hosts|sudoers|pam\.d)|/library/scripts|"
    r"/library/(?:application support|preferences|internet plug-ins|input methods|services|quicklook|spotlight)|"
    r"com\.apple\.loginwindow|com\.apple\.loginitems|lsquarantine|"
    r"(?:~|\$\{?home\}?|/users/[^/\s]+)/\.(?:zshrc|zshenv|zprofile|zlogin|zlogout|bashrc|bash_profile|bash_login|profile|"
    r"inputrc|vimrc|gitconfig|config/(?:git|fish|autostart)|ssh|npmrc|curlrc|wgetrc|tmux\.conf|lldbinit|gdbinit|pythonrc|"
    r"hushlogin|xinitrc|login_conf|launchd\.conf|cshrc|tcshrc|kshrc|mkshrc|oh-my-zsh)|"
    r"\.git/hooks|core\.hookspath|core\.fsmonitor|core\.sshcommand|filter\.[^\s.]+\.(?:clean|smudge|process))"
)
HOME_LIBRARY_RE = re.compile(r"(?:~|\$\{?home\}?|/users/[^/\s]+)/library\b")
HOME_DOT_RE = re.compile(r"(?:~|\$\{?home\}?|/users/[^/\s]+)/\.[a-z0-9_]")
LEUTHERIA_STATE_RE = re.compile(
    r"(?:trusted_commands\.json|voice_settings\.json|home_location\.json|system_prompt\.md|skills/generated|"
    r"events\.jsonl|application support/leutheria|leutheria_[a-z_]+|(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)[: ]8765|"
    r"\b8765\b|agent/agent/|tool_catalog\.json|registry_keys\.py|preferences\.json)"
)
EXFIL_HOST_RE = re.compile(
    r"(?:webhook\.site|requestbin|pipedream|\.ngrok|ngrok\.io|ngrok-free|interact\.sh|oast\.|burpcollaborator|"
    r"canarytokens|discord(?:app)?\.com/api/webhooks|api\.telegram\.org|hooks\.slack\.com|transfer\.sh|"
    r"0x0\.st|file\.io|termbin|ix\.io|pastebin\.com|paste\.ee|hastebin|ghostbin|trycloudflare\.com|serveo|localtunnel|"
    r"loca\.lt|beeceptor|mockbin|postb\.in|dnslog|ceye\.io|requestcatcher)"
)
SHORTENER_RE = re.compile(r"(?:bit\.ly|tinyurl\.com|t\.co/|goo\.gl|is\.gd|cutt\.ly|rb\.gy|ow\.ly|buff\.ly|shorturl\.at|tiny\.cc)")
EXEC_EXT_RE = re.compile(
    r"\.(?:command|tool|sh|zsh|bash|csh|ksh|fish|py|pyc|rb|pl|php|js|jxa|scpt|scptd|applescript|app|pkg|mpkg|dmg|"
    r"plist|terminal|workflow|action|shortcut|webloc|inetloc|fileloc|jar|bin|run|out|so|dylib|bundle|kext|prefpane|"
    r"saver|qlgenerator|mdimporter|osax|definition|configprofile|mobileconfig)\b"
)
ARCHIVE_EXT_RE = re.compile(r"\.(?:zip|tar|tgz|gz|bz2|xz|7z|rar|dmg|pkg|iso|cpio|xip)\b")
BROAD_TARGETS = {
    "/", "/*", "~", "~/", "~/*", "~/.*", "*", ".*", ".", "./", "./*", "..", "../", "../*", "$home", "${home}", "$home/",
    "${home}/", "$home/*", "${home}/*", "/users", "/users/", "/users/*", "/applications", "/applications/*",
    "/system", "/library", "/usr", "/bin", "/sbin", "/etc", "/var", "/private", "/opt", "/volumes", "/volumes/*",
    "/cores", "/private/var", "--no-preserve-root",
}
HOME_TOP = ("documents", "desktop", "downloads", "pictures", "movies", "music", "library", "applications", "public", "projects")


def _is_broad(target: str) -> bool:
    t = target.strip().rstrip()
    if t in BROAD_TARGETS:
        return True
    m = re.fullmatch(r"(?:~|\$\{?home\}?|/users/[^/]+)/([^/]+)/?\*?", t)
    if m and m.group(1) in HOME_TOP:
        return True
    if re.fullmatch(r"/users/[^/]+/?\*?", t):
        return True
    return bool(re.fullmatch(r"/[^/]+/?\*", t))


# --- shell analysis -----------------------------------------------------------------------

def analyze_shell(raw: str, where: str, F: _Findings, *, depth: int = 0, ctx: dict | None = None) -> dict:
    """All shell rules over one string. Returns facts the cross-step rules use."""
    ctx = ctx if ctx is not None else {}
    facts = {"programs": set(), "fetch": False, "writes": False, "exec_file": False, "deletes": False, "network": False}
    if not raw or not isinstance(raw, str):
        return facts

    sev, label = hidden_chars(raw)
    if sev:
        F.add(where, "hidden_chars", BLOCK,
              f"command contains {label}: the confirmation card would not show what actually runs")
    if any(ord(c) > 126 for c in strip_hidden(raw)):
        F.add(where, "non_ascii_command", REVIEW, "command contains non-ASCII characters (look-alike letters can disguise a program or path)")

    views = shell_views(raw)
    lit, deq, deob = views["literal"], views["dequoted"], views["deobfuscated"]
    allv = (lit, deq, deob)

    # -- obfuscation indicators on the literal text --
    if re.search(r"\$'[^']*\\(?:x[0-9a-f]|[0-7]{3}|u[0-9a-f]{4})", lit) or re.search(r"\\x[0-9a-f]{2}", lit):
        F.add(where, "escaped_bytes", REVIEW, "command spells characters as escape codes")
    if re.search(r"[a-z0-9](?:''|\"\")[a-z0-9]|[a-z0-9]['\"][a-z0-9]{1,3}['\"][a-z0-9]", lit):
        F.add(where, "evasive_quoting", REVIEW, "quotes split a word, a common way to hide a program name from pattern matching")
    if re.search(r"(?:^|[\s;|&(])[a-z]*\\[a-z]", lit):
        F.add(where, "evasive_backslash", REVIEW, "backslashes inside a word, a common way to hide a program name")
    if re.search(r"\$\{?ifs\b", lit):
        F.add(where, "ifs_trick", BLOCK, "uses $IFS as whitespace, an evasion technique with no place in a shared skill")
    if re.search(r"\$\{![a-z_]|\$\{[a-z_]\w*:[-+=?]?\s*-?\d|\$\{[a-z_]\w*(?:/|//|\^|,|#|%)", lit):
        F.add(where, "parameter_expansion_trick", BLOCK, "uses indirection or substring/replace expansion to build text at run time")

    # -- program inventory (all views, plus globs) --
    progs = set()
    globbed = False
    for v in allv:
        for t in tokens(v):
            b = basename(t)
            progs.add(b)
            if re.search(r"[*?\[]", b) and len(re.sub(r"[^a-z0-9]", "", b)) >= 2 and len(b) <= 24:
                for name in GLOB_TARGETS:
                    if fnmatch.fnmatchcase(name, b):
                        progs.add(name)
                        globbed = True
    progs = {re.sub(r"^(python|pip|ruby|perl|php|node)[0-9.]*$", r"\1", p) for p in progs}
    facts["programs"] = progs

    # -- computed program names --
    raw_cmd_words = command_words(deq)
    for w in raw_cmd_words:
        if w.startswith(PARAM_MARK.lower()):
            F.add(where, "param_is_program", BLOCK, "a parameter chooses which program runs")
            continue
        dyn = re.sub(r"^(?:~|\$\{?home\}?)(?=/)", "", w)
        if "$" in dyn or "`" in dyn:
            F.add(where, "dynamic_program", BLOCK, "the program name is computed at run time (variable or command substitution), so nobody can see what runs")
        if re.search(r"[*?\[]", w):
            F.add(where, "globbed_program", BLOCK, "the program name is a wildcard pattern, hiding which program runs")
    if globbed:
        F.add(where, "globbed_program", BLOCK, "a wildcard pattern resolves to a sensitive program")
    if re.search(r"(?:^|[;&|(\s])(?:exec|eval)\s+\$|(?:^|\s)\$\(\s*(?:echo|printf|cat|rev|base64|xxd|tr|sed|awk)\b[^)]*\)\s*[a-z/]", deq):
        F.add(where, "dynamic_program", BLOCK, "builds the command text at run time")

    # -- execution of strings --
    pipe_interp = re.compile(
        r"\|\s*(?:(?:sudo|env|exec|command|nohup|time|xargs(?:\s+-\S+)*)\s+)*(?:\S*/)?"
        r"(?:sh|bash|zsh|dash|ksh|fish|csh|tcsh|python[0-9.]*|perl|ruby|node|php|lua|osascript|swift|tclsh|source|eval|\.)(?:\s|$|;|\))(?!\s*-m\s+json\.tool\b)"
    )
    if any(pipe_interp.search(v) for v in allv):
        F.add(where, "pipe_to_interpreter", BLOCK, "pipes text into a shell or interpreter (download-and-execute / hidden payload)")
    if any(re.search(r"(?:^|[\s;|&(])(?:sh|bash|zsh|source|\.|python[0-9.]*|perl|ruby|node)\s+(?:-\S+\s+)*<\(", v) for v in allv):
        F.add(where, "process_substitution_exec", BLOCK, "runs the output of another command as a script")
    if any(re.search(r"<<<?\s*\S*", v) and re.search(r"(?:^|[\s;|&(])(?:sh|bash|zsh|python[0-9.]*|perl|ruby|node|osascript)\s*(?:-\S+\s*)*<<", v) for v in allv):
        F.add(where, "heredoc_exec", BLOCK, "feeds an inline script to an interpreter on stdin")
    if "eval" in progs:
        F.add(where, "eval", BLOCK, "uses eval to run text as code")
    inline = any(re.search(r"(?:^|[\s;|&(/])(?:sh|bash|zsh|dash|ksh|fish|csh)\s+(?:-\S+\s+)*-[a-z]*c\b|"
                           r"(?:python[0-9.]*|perl|ruby|node|php|lua|deno|bun|swift)\s+(?:-\S+\s+)*-[a-z]*[ecr]\b|"
                           r"\bosascript\b|\bawk\b[^|;]*\bsystem\s*\(", v) for v in allv)
    if inline:
        F.add(where, "inline_interpreter", REVIEW, "runs inline code through a shell or interpreter")
    if {"source", "."} & set(raw_cmd_words) or re.search(r"(?:^|[;&|]\s*)(?:source|\.)\s+\S", deq):
        F.add(where, "source_file", REVIEW, "sources a file into the shell")

    decoder = bool(progs & {"base64", "base32", "uudecode", "basenc"}) or bool(
        re.search(r"\bxxd\b[^|;]*-r|\bopenssl\b[^|;]*(?:-d\b|base64|enc)|\bbase64\b[^|;]*(?:-d|-D|--decode)|\bprintf\b[^|;]*(?:%b|\\x)|\becho\s+-e\b|\bb64decode|frombase64|\bcodecs\.decode|\bbytes\.fromhex|\bunhexlify|\bchr\s*\(", lit + " " + deob)
    )
    if decoder:
        F.add(where, "decoder", REVIEW, "decodes encoded data at run time")
        if inline or "eval" in progs or any(pipe_interp.search(v) for v in allv) or re.search(
                r"(?:\$\(|`)[^)`]*(?:base64|base32|xxd|openssl|printf|echo\s+-e|rev\b|uudecode|tr\s)", lit + " " + deq):
            F.add(where, "decode_and_execute", BLOCK, "decodes data and executes it")
    if "rev" in progs or re.search(r"\btr\s+['\"]?[a-z]-[a-z]", lit):
        F.add(where, "text_scrambling", REVIEW, "reverses or translates text at run time, a way to hide a command")

    # -- embedded encoded payloads --
    if depth < 3:
        for blob in decoded_blobs(strip_hidden(raw)):
            F.add(where, "encoded_payload", REVIEW, "contains text hidden in base64/hex encoding")
            sub = analyze_shell(blob, where + " (decoded)", F, depth=depth + 1, ctx=ctx)
            for k in ("fetch", "writes", "exec_file", "deletes", "network"):
                facts[k] = facts[k] or sub[k]
            facts["programs"] |= sub["programs"]

    # -- network --
    fetch = bool(progs & FETCHERS) or bool(re.search(r"\burllib|\brequests\.(?:get|post)|\bhttp\.client|\bsocket\.|\bnet/http|\bopen-uri|\bfetch\(|nsurl|curl_init|file_get_contents\(\s*['\"]?https?", lit + " " + deob))
    raw_net = bool(progs & RAW_NET) or "/dev/tcp/" in deob or "/dev/udp/" in deob
    facts["fetch"] = facts["fetch"] or fetch
    facts["network"] = facts["network"] or fetch or raw_net or bool(progs & (DNS_PING | MAILERS))
    if raw_net:
        F.add(where, "raw_socket", BLOCK, "opens a raw network connection (netcat, socat, /dev/tcp): reverse shells and data exfiltration")
    if fetch:
        F.add(where, "network_fetch", REVIEW, "downloads from or uploads to the network")
    if re.search(r"\bgit\s+(?:clone|pull|fetch|push|remote|submodule|ls-remote)\b", deob):
        facts["network"] = True
    if re.search(r"\b(?:brew|npm|npx|pip|pip3|gem|cargo|go|pnpm|yarn|port|mas|softwareupdate)\s+(?:install|update|upgrade|add|get|i|outdated|fetch)\b", deob):
        facts["network"] = True
        F.add(where, "package_manager", INFO, "installs or updates packages (runs code from the package source)")
    if re.search(r"\bgit\s+(?:clone|submodule)\b", deob):
        F.add(where, "git_clone", REVIEW, "clones a repository (its hooks and config come from a stranger)")
    if re.search(r"\b(?:npx|pipx\s+run|uvx|bunx|pnpm\s+dlx)\b", deob):
        F.add(where, "remote_package_exec", BLOCK, "downloads and runs a package in one step")

    # -- execution of files (staging) --
    chmod_x = bool(re.search(r"\bchmod\b[^;|&]*(?:\+[rwx]*x|[0-7]?[1357][0-7]{2}\b|a\+x|u\+x)", deob))
    quarantine = bool(re.search(r"\bxattr\b[^;|&]*(?:-[a-z]*[dcr]|quarantine)", deob)) or "lsquarantine" in deob
    exec_path = any(re.match(r"(?:\./|/tmp/|/private/|/var/folders/|/users/shared/|~/|\$\{?home\}?/|/volumes/)", w)
                    and not re.match(r"(?:~|\$\{?home\}?)/(?:\.local/bin|bin)/", w) for w in command_words(deob))
    runs_script = any(re.search(r"(?:^|[\s;|&(/])(?:sh|bash|zsh|python[0-9.]*|perl|ruby|node|osascript|php)\s+(?:-\S+\s+)*[~./$a-z]", v) for v in (deq, deob))
    opener = bool(re.search(r"(?:^|[\s;|&(/])open\s+(?!-a\s)", deob)) or bool(progs & {"installer", "hdiutil", "pkgutil"})
    facts["exec_file"] = facts["exec_file"] or chmod_x or exec_path or runs_script or opener
    if quarantine:
        F.add(where, "quarantine_removal", BLOCK, "removes the quarantine mark that makes macOS check downloaded code")
    if chmod_x:
        F.add(where, "make_executable", REVIEW, "marks a file executable")
    if fetch and (chmod_x or exec_path or runs_script or opener or quarantine):
        F.add(where, "download_and_execute", BLOCK, "downloads something and executes or opens it")
    if exec_path:
        F.add(where, "runs_local_file", REVIEW, "runs a program from a user or temporary folder")

    # -- exfiltration --
    subst = bool(re.search(r"\$\(|`|<\(", lit + deq))
    env_ref = bool(ENV_VAR_RE.search(deq))
    upload = bool(re.search(r"(?:\s-d\s*@|--data(?:-binary|-raw|-urlencode)?\s*[=\s]?@|\s-f\s+\S*=@|\s-t\s+\S|--upload-file|--form\s+\S*@|-x\s*(?:post|put)|--request\s+(?:post|put)|\s-d\s|--data\b|--post-(?:file|data))", deob))
    reads_local = bool(re.search(r"\b(?:cat|pbpaste|env|printenv|security|screencapture|ls|find|tar|zip|head|tail|defaults|sqlite3|ditto|base64|history|ps|ifconfig|whoami|hostname|system_profiler)\b[^|;&]*\|\s*(?:\S*/)?(?:curl|wget|nc|ncat|netcat|socat|telnet|mail|mailx|sendmail|ssh|scp|openssl|xargs)", deob))
    if (fetch or raw_net or progs & DNS_PING or progs & MAILERS) and (subst or env_ref or upload or reads_local):
        F.add(where, "exfiltration", BLOCK, "sends local data (command output, environment, files) to the network")
    if progs & MAILERS:
        F.add(where, "sends_mail", REVIEW, "sends email from the command line")
    if progs & {"ssh", "scp", "sftp", "rsync"} and re.search(r"[a-z0-9_.-]+@[a-z0-9.-]+|\b[a-z0-9.-]+\.[a-z]{2,}:", deob):
        F.add(where, "remote_copy", REVIEW, "connects to a remote machine")
    if re.search(r"https?://[^\s]*(?:\$\(|`|\$\{?[a-z_])", deq):
        F.add(where, "url_substitution", BLOCK, "builds a URL from command output or environment variables")

    # -- secrets --
    if any(SECRET_RE.search(v) for v in allv):
        F.add(where, "secret_access", BLOCK, "touches credentials (SSH keys, keychain, cloud/API credentials, browser data, shell history)")
    if any(SECRET_ENV_RE.search(v) for v in (deq, deob)):
        F.add(where, "secret_env", BLOCK, "reads secret-looking environment variables")
    if re.search(r"(?:^|[\s;|&(])(?:env|printenv|set|export\s+-p|declare\s+-[px])\s*(?:$|[|;>&)])", deob):
        F.add(where, "env_dump", REVIEW, "dumps the environment (API keys live there)")
    if "security" in progs and re.search(r"\bsecurity\s+(?:find|dump|export|unlock|add|delete|set|import|authorizationdb|-)", deob):
        F.add(where, "keychain", BLOCK, "uses the security tool to reach the keychain")

    # -- privilege / scripting hosts / evasion --
    if progs & PRIV or re.search(r"with administrator privileges", deob):
        F.add(where, "privilege", BLOCK, "asks for administrator privileges")
    if "osascript" in progs or re.search(r"\bjxa\b|tell application|do shell script|system events", deob):
        F.add(where, "osascript", BLOCK, "runs AppleScript/JXA, which can drive any app and bypass the GUI-control limits")
    if progs & {"automator", "shortcuts"}:
        F.add(where, "script_host", REVIEW, "runs an Automator workflow or Shortcut whose contents aren't in the skill")
    if progs & EVASION or re.search(r"\b(?:tcc\.db|kTCC|gatekeeper|assessments?\.enable|syspolicy)\b", deob):
        F.add(where, "defense_evasion", BLOCK, "changes macOS security settings (Gatekeeper, SIP, privacy database, firmware)")
    if re.search(r"\bnetworksetup\b[^;|&]*-set(?:web|secureweb|socksfirewall|autoproxy|dns|proxy)", deob) or re.search(r"/etc/hosts|/etc/resolv", deob):
        F.add(where, "network_hijack", BLOCK, "changes proxy, DNS or hosts settings")
    if "pmset" in progs or "networksetup" in progs or "scutil" in progs:
        F.add(where, "system_settings", REVIEW, "changes system power or network settings")

    # -- persistence --
    writes = bool(progs & WRITER_PROGS) or bool(re.search(r">|\bsed\s+-i|\bperl\s+-[a-z]*i|\bdefaults\s+write", deq))
    strong_writes = bool(progs & {"tee", "cp", "mv", "ln", "install", "ditto", "rsync", "touch", "mkdir", "unzip", "plutil"}) or bool(
        re.search(r">|\bsed\s+-i|\bdefaults\s+write|\btar\s+-?[a-z]*x|\bcurl\b[^;|&]*\s-[a-z]*o|\bwget\b|\bgit\s+(?:clone|pull|init|commit|checkout|merge)", deq))
    facts["writes"] = facts["writes"] or strong_writes
    if progs & PERSIST_PROGS - {"at", "batch"} or re.search(r"(?:^|[;&|]\s*)at\s+(?:now|\d|-f|midnight|noon|teatime)", deob):
        F.add(where, "persistence", BLOCK, "schedules code to run later (launchctl, cron, at, periodic)")
    if any(PERSIST_PATH_RE.search(v) for v in allv):
        F.add(where, "persistence", BLOCK if writes or chmod_x else REVIEW,
              "touches a login/startup location, shell startup file, git hook or launch agent")
    if any(HOME_LIBRARY_RE.search(v) for v in allv):
        F.add(where, "home_library", BLOCK if writes else REVIEW, "touches ~/Library (preferences, app data, launch agents)")
    elif any(HOME_DOT_RE.search(v) for v in allv):
        F.add(where, "home_dotfile", BLOCK if writes else REVIEW, "touches a hidden file or folder in the home directory")
    if re.search(r"\bdefaults\s+write\b", deob):
        F.add(where, "defaults_write", REVIEW, "changes an app's preferences")
    if re.search(r"\bgit\s+config\b[^;|&]*(?:--global|--system|core\.|alias\.|credential|url\.|include\.|filter\.)", deob):
        F.add(where, "git_config", BLOCK, "changes git configuration that can run commands later")

    # -- the agent itself --
    if any(LEUTHERIA_STATE_RE.search(v) for v in allv):
        F.add(where, "agent_tamper", BLOCK, "touches Leutheria's own state, settings, token or socket")
    elif "leutheria" in deob:
        F.add(where, "agent_mention", REVIEW, "mentions Leutheria's install")
    if re.search(r"\b(?:pkill|killall)\b[^;|&]*(?:python|electron|leutheria|-f\s)", deob):
        F.add(where, "kills_agent", REVIEW, "kills processes by pattern")

    # -- destruction --
    deletes = False
    for seg in SEP_RE.split(deob):
        toks = seg.split()
        if not toks:
            continue
        progs_in_seg = [basename(t) for t in toks]
        for idx, p in enumerate(progs_in_seg):
            if p in ("rm", "srm", "unlink", "rmdir", "trash"):
                args = toks[idx + 1:]
                flags = "".join(a.lstrip("-") for a in args if a.startswith("-"))
                targets = [a for a in args if not a.startswith("-") or a == "--no-preserve-root"]
                deletes = True
                if any(_is_broad(t) for t in targets) or "--no-preserve-root" in args:
                    F.add(where, "mass_delete", BLOCK, "deletes the home folder, the disk root, a top-level folder or a wildcard of them")
                elif "r" in flags.lower() or "recursive" in flags:
                    F.add(where, "recursive_delete", REVIEW, "deletes folders recursively (trash_file is the recoverable way)")
                else:
                    F.add(where, "delete", REVIEW, "deletes files permanently (trash_file is the recoverable way)")
                break
            if p == "find" and re.search(r"-delete|-exec\s+(?:\S*/)?(?:rm|srm|shred)|-execdir\s+(?:\S*/)?rm", seg):
                deletes = True
                root = toks[idx + 1] if idx + 1 < len(toks) else ""
                F.add(where, "find_delete", BLOCK if root in ("/", "~", "~/", "$home", "${home}", "/users", "/volumes") or root.startswith("-") else REVIEW,
                      "deletes whatever a find matches")
                break
    if not deletes and re.search(r"\b(?:rm|srm|unlink|shred)\b", deob):
        deletes = True
        F.add(where, "delete", REVIEW, "deletes files")
    facts["deletes"] = facts["deletes"] or deletes
    if progs & DISK or re.search(r"\bdd\b[^;|&]*of=/dev/|>\s*/dev/(?:r?disk|sd)", deob):
        F.add(where, "disk_destroy", BLOCK, "erases or overwrites disks or securely shreds data")
    if re.search(r"([a-z_:]\w*|:)\s*\(\)\s*\{[^}]*\1\s*\|\s*\1", deob):
        F.add(where, "fork_bomb", BLOCK, "defines a fork bomb")
    if re.search(r"\b(?:chmod|chown|chflags)\s+-[a-z]*r[a-z]*\s+\S+\s+(\S+)", deob):
        tgt = re.search(r"\b(?:chmod|chown|chflags)\s+-[a-z]*r[a-z]*\s+\S+\s+(\S+)", deob).group(1)
        F.add(where, "recursive_permissions", BLOCK if _is_broad(tgt) else REVIEW, "changes ownership/permissions recursively")
    if re.search(r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f|push\s+[^;|&]*(?:--force|-f\b)|checkout\s+--\s+\.|branch\s+-d)", deob):
        F.add(where, "git_destructive", REVIEW, "discards or overwrites git history or working-tree changes")
    if re.search(r"\b(?:shutdown|reboot|halt)\b", deob):
        F.add(where, "power", REVIEW, "shuts down or restarts the Mac")
    if re.search(r">\s*(?:~|\$\{?home\}?|/users)/[^\s]*", deq) and not re.search(r">>", deq):
        F.add(where, "overwrite_redirect", REVIEW, "overwrites a file with shell redirection")

    if len(raw) > 400:
        F.add(where, "long_command", REVIEW, "command is too long to judge from a confirmation card")
    if re.search(r"(?:^|[\s=/'\"])" + PARAM_MARK.lower() + r"_", deob):
        F.add(where, "param_in_command", INFO, "a parameter value is inserted into the command (the confirmation card shows the final text)")
    return facts


# --- prose (description, parameter descriptions, enum/default values) ------------------

CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d",
    "ɡ": "g", "һ": "h", "ӏ": "l", "ո": "n", "ս": "u", "ν": "v", "ο": "o", "α": "a", "ε": "e", "ι": "i", "κ": "k",
    "ρ": "p", "τ": "t", "υ": "u", "ѡ": "w", "ꮃ": "w", "Ꭵ": "i", "ı": "i", "ℓ": "l",
})
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "|": "l", "!": "i"})


def prose_views(text: str) -> list:
    t = unicodedata.normalize("NFKC", strip_hidden(text)).translate(CONFUSABLES).lower()
    flat = re.sub(r"\s+", " ", t)
    # i g n o r e / i.g.n.o.r.e / i-g-n-o-r-e -> ignore
    joined = re.sub(r"\b(?:[a-z][\s.\-_*/]){3,}[a-z]\b", lambda m: re.sub(r"[\s.\-_*/]", "", m.group(0)), flat)
    views = [flat, joined, flat.translate(LEET), flat[::-1], codecs.decode(flat, "rot13"),
             re.sub(r"[_\-]+", " ", flat)]
    for blob in decoded_blobs(strip_hidden(text)):
        views.append(re.sub(r"\s+", " ", blob.lower()))
    return views


INJECTION_BLOCK = [
    ("override", r"\b(?:ignore|disregard|forget|override|bypass|skip)\b.{0,30}\b(?:previous|prior|above|earlier|preceding|all|any|other|your|system|safety|security)\b.{0,30}\b(?:instructions?|rules?|prompts?|directions?|guidelines?|messages?|polic(?:y|ies)|constraints?|checks?)\b"),
    ("override", r"\b(?:new|updated|revised|real|actual|true)\s+(?:system\s+)?(?:instructions?|rules|directives?|policy)\s*[:\-]"),
    ("role_marker", r"<\s*/?\s*(?:system|assistant|user|human|instructions?|untrusted_skill|skill|tool_result|function_results?|function_calls|antml|im_start|im_end|developer|admin)\b"),
    ("role_marker", r"(?:^|[.\n!?]\s*)(?:system|assistant|human|developer|admin)\s*:"),
    ("role_marker", r"\[/?inst\]|<<\s*/?sys\s*>>|<\|[a-z_]+\|>|###\s*(?:system|instruction|response)"),
    ("screener_targeting", r"\b(?:screener|reviewer|moderator|auditor|grader|classifier|evaluator|scanner|security review|static analy[sz]er|language model|llm)\b.{0,80}\b(?:approve|approved|verdict|safe|clean|low risk|benign)\b"),
    ("screener_targeting", r"\b(?:verdict|risk)\s*[:=]\s*[\"']?(?:approve|low|safe|clean)|\{\s*[\"']?verdict|\bauto-?approve|\bpre-?approved\b|\balready\s+(?:been\s+)?(?:reviewed|approved|vetted|audited|verified|screened)\b|\bmark\s+(?:this|it)\s+(?:as\s+)?(?:safe|clean|approved|benign)\b"),
    ("silence_user", r"\b(?:do not|don'?t|never|no need to)\s+(?:ask|confirm|tell|inform|notify|mention|warn|show|alert|bother)\b.{0,20}\b(?:user|anyone|confirmation|them|human|owner)\b"),
    ("silence_user", r"\bwithout\s+(?:asking|confirmation|confirming|approval|telling|notifying|the user|user)\b|\bsilently\b|\bin the background without\b"),
    ("hijack_routing", r"\b(?:always|must|should)\s+(?:be\s+)?(?:call(?:ed)?|use[d]?|run|invoke[d]?|prefer(?:red)?|choose|chosen|select(?:ed)?|pick(?:ed)?|trigger(?:ed)?)\b.{0,20}\b(?:this|me|first)\b"),
    ("hijack_routing", r"\b(?:call|use|run|invoke)\s+this\s+(?:skill|tool|function)?\s*(?:first|before|for (?:every|all|any)|instead|whenever|on every|regardless)"),
    ("hijack_routing", r"\bbefore\s+(?:any|every|each|all)\s+(?:other\s+)?(?:tool|request|action|response|reply|task|command)s?\b|\binstead of (?:any|all|every|the) (?:other )?(?:tools?|skills?|functions?)\b|\bfor (?:every|each|all|any) (?:user )?(?:request|query|message|task|conversation)s?\b"),
    ("secret_request", r"\b(?:send|paste|include|read|share|upload|post|forward|reveal|print|output|leak)\b.{0,40}\b(?:api[ _-]?keys?|passwords?|tokens?|credentials?|ssh keys?|secrets?|private keys?|\.env|system prompt|cookies?)\b"),
    ("secret_request", r"\b(?:api[ _-]?keys?|passwords?|tokens?|credentials?|ssh keys?|secrets?|private keys?|system prompt)\b.{0,40}\b(?:send|paste|upload|post|forward|reveal|leak|exfiltrat)\w*"),
    ("persona_hijack", r"\byou are (?:now|no longer)\b|\bact as (?:an? )?(?:unrestricted|jailbroken|dan|developer mode)|\bdeveloper mode\b|\bjailbreak"),
]
INJECTION_REVIEW = [
    ("model_address", r"\b(?:note|message|attention|instructions?)\s+(?:to|for)\s+(?:the\s+)?(?:ai|assistant|model|llm|claude|agent|bot)\b|\b(?:dear|hey|hello)\s+(?:ai|assistant|model|claude|agent)\b"),
    ("imperative", r"\byou\s+(?:must|should always|need to|have to|are required to|shall|will now)\b|\b(?:important|critical|urgent|mandatory)\s*[:!]"),
    ("prompt_words", r"\bsystem prompt\b|\bprompt injection\b|\b(?:your|the) instructions\b|\btool list\b"),
    ("always_never", r"\b(?:always|never)\s+(?:do|say|respond|reply|answer|tell|ask|run|open|call|use)\b"),
    ("output_format", r"\b(?:respond|reply|answer|output)\s+(?:only\s+)?(?:with|exactly)\b|\breturn\s+(?:only\s+)?(?:the\s+)?json\b"),
    ("confirmation_words", r"\b(?:say|answer|reply)\s+(?:yes|confirm|approve)\b|\bapprove (?:it|this|all)\b|\btrust (?:it|this) always\b"),
]


def analyze_prose(text: str, where: str, F: _Findings):
    if not isinstance(text, str) or not text:
        return
    sev, label = hidden_chars(text)
    if sev:
        F.add(where, "hidden_chars", sev, f"prose contains {label}")
    views = prose_views(text)
    for i, v in enumerate(views):
        for rule, pat in INJECTION_BLOCK:
            if re.search(pat, v):
                note = "" if i == 0 else " (found after undoing obfuscation)"
                F.add(where, "injection_" + rule, BLOCK,
                      f"instruction-like text aimed at the model or the screener ({rule.replace('_', ' ')}){note}")
        for rule, pat in INJECTION_REVIEW:
            if re.search(pat, v):
                F.add(where, "injection_" + rule, REVIEW, f"instruction-like text ({rule.replace('_', ' ')})")
    if re.search(r"https?://|www\.", views[0]):
        F.add(where, "prose_url", INFO, "prose contains a URL (stripped from the description the model sees)")
    if re.search(r"[`#*_]{2,}|\[[^\]]+\]\([^)]+\)|^\s*[-*#>]", views[0], re.M):
        F.add(where, "prose_markdown", INFO, "prose contains markdown (stripped locally)")
    if any(ord(c) > 0x2FF for c in strip_hidden(text)) and re.search(r"[a-z]", views[0]):
        if unicodedata.normalize("NFKC", strip_hidden(text)).lower().translate(CONFUSABLES) != unicodedata.normalize("NFKC", strip_hidden(text)).lower():
            F.add(where, "confusable_letters", REVIEW, "prose mixes in look-alike letters from other scripts")


# --- auto-publish allowlist for run_command ---------------------------------------------------
#
# The rules above are a blocklist: they catch what they name. Whole classes
# get no finding from them -- options of an otherwise harmless program that
# write a file or name another program to run, subcommands that fetch and
# evaluate third-party code, a parameter filled in at run time, a second
# command chained onto the first -- so on their own they would leave the AI
# screener as the only gate on auto-publishing such a skill. This allowlist
# closes that: a run_command step can be auto-published only if every
# command it can become is one simple command (no shell syntax at all), of a
# listed program, with only listed subcommands and option shapes, and no
# parameter in it. Anything else is at least needs_review, so a person reads
# it. It never blocks and never loosens a block; widening it is a reviewed
# change here, with a case in the eval. Program names are bare words: a path
# to a program is not the program.

ALLOW_CMD_MAX = 200
ALLOW_CHARS_RE = re.compile(r"[A-Za-z0-9 _./:=@,+%~-]+")
ALLOW_OPERAND_RE = re.compile(r"[A-Za-z0-9_.~/:@,+%=-]+")
BREW_NAME_RE = re.compile(r"[a-z0-9][a-z0-9@._+-]{0,99}")  # no "/": a tap-qualified name fetches a third party's code

# git: read-only subcommands; no global options (they choose the repository,
# config or programs git runs). Values: exact options, or "prefix=" for an
# option that takes its value in the same word.
GIT_ALLOW = {
    "status": ({"-s", "--short", "-b", "--branch", "-sb", "--porcelain", "--porcelain=v1", "--porcelain=v2"}, False),
    "log": ({"--oneline", "--graph", "--decorate", "--all", "--stat", "--shortstat", "--name-only", "--name-status",
             "--reverse", "--no-merges", "--merges", "--first-parent", "--since=", "--until=", "--after=",
             "--before=", "--author=", "--max-count=", "--date="}, True),
    "diff": ({"--stat", "--shortstat", "--numstat", "--name-only", "--name-status", "--cached", "--staged"}, True),
    "show": ({"--stat", "--name-only", "--name-status", "--oneline", "--no-patch", "-s"}, True),
    "branch": ({"-a", "-r", "-v", "-vv", "--list", "--show-current", "--merged", "--no-merged"}, False),
    "remote": ({"-v"}, False),
    "tag": ({"-l", "--list"}, False),
    "rev-parse": ({"--abbrev-ref", "--short", "--show-toplevel", "--is-inside-work-tree"}, True),
    "describe": ({"--tags", "--always", "--abbrev="}, True),
    "shortlog": ({"-s", "-n", "-sn", "-e", "--summary", "--numbered"}, False),
}
BREW_ALLOW = {
    "info": {"--formula", "--cask", "--json=v2", "--installed"},
    "list": {"--formula", "--cask", "--versions", "-1"},
    "ls": {"--formula", "--cask", "--versions", "-1"},
    "outdated": {"--formula", "--cask", "--quiet", "-q", "--verbose", "-v"},
    "search": {"--formula", "--cask"},
    "deps": {"--tree", "--installed"},
    "uses": {"--installed"},
    "desc": set(),
    "leaves": set(),
    "config": set(),
    "update": set(),
}
BREW_NO_OPERANDS = {"leaves", "config", "update"}


def _opt_ok(tok: str, allowed: set) -> bool:
    if tok in allowed:
        return True
    return any(a.endswith("=") and tok.startswith(a) and len(tok) > len(a) for a in allowed)


def _git_ok(args: list) -> str | None:
    if not args or args[0] not in GIT_ALLOW:
        return "git with a subcommand that is not on the read-only list, or with global options"
    allowed, operands = GIT_ALLOW[args[0]]
    rest, after_dashes = args[1:], False
    i = 0
    while i < len(rest):
        t = rest[i]
        if t == "--" and operands and not after_dashes:
            after_dashes = True
        elif t.startswith("-") and not after_dashes:
            if args[0] == "log" and (re.fullmatch(r"-\d{1,6}", t) or re.fullmatch(r"-n\d{1,6}", t)):
                pass
            elif args[0] == "log" and t == "-n" and i + 1 < len(rest) and re.fullmatch(r"\d{1,6}", rest[i + 1]):
                i += 1
            elif not _opt_ok(t, allowed):
                return "git with an option that is not on the list for its subcommand"
        elif not operands:
            return "git subcommand given an operand it would act on"
        i += 1
    return None


def _brew_ok(args: list) -> str | None:
    if args == ["--version"]:
        return None
    if not args or args[0] not in BREW_ALLOW:
        return "brew with a subcommand that is not on the read-only list"
    for t in args[1:]:
        if t.startswith("-"):
            if t not in BREW_ALLOW[args[0]]:
                return "brew with an option that is not on the list for its subcommand"
        elif args[0] in BREW_NO_OPERANDS or not BREW_NAME_RE.fullmatch(t):
            return "brew given a name that is not a plain formula or cask name"
    return None


def _only(opts_re: str, operands: bool):
    def check(args):
        for t in args:
            if t.startswith("-"):
                if not re.fullmatch(opts_re, t):
                    return "an option that is not on the list for this program"
            elif not operands:
                return "an operand this program would act on"
        return None
    return check


def _date_ok(args):
    for t in args:
        if t in ("-u", "-R") or (t.startswith("+") and len(t) > 1):
            continue
        return "date with something other than -u, -R or a +format (an operand sets the clock)"
    return None


def _echo_ok(args):
    if any(t.startswith("-") and t != "-n" for t in args):
        return "echo with an option other than -n"
    return None


ALLOWED_PROGRAMS = {
    "echo": _echo_ok,
    "date": _date_ok,
    "pwd": _only(r"(?!)", False),
    "whoami": _only(r"(?!)", False),
    "uptime": _only(r"(?!)", False),
    "sw_vers": _only(r"--?(?:productName|productVersion|buildVersion)", False),
    "uname": _only(r"-[amnprsv]{1,7}", False),
    "ls": _only(r"-[A-Za-z1@]{1,12}", True),
    "df": _only(r"-[hHklPi]{1,6}", True),
    "du": _only(r"-[schkx]{1,5}", True),
    "which": _only(r"-a", True),
    "git": _git_ok,
    "brew": _brew_ok,
}


def allowlist_problem(cmd: str) -> str | None:
    """None if `cmd` (one fully substituted command) may be auto-published;
    otherwise a fixed reason (never the command's own text)."""
    if not isinstance(cmd, str) or not cmd.strip():
        return "empty command"
    if len(cmd) > ALLOW_CMD_MAX:
        return "longer than the auto-publish limit"
    if PARAM_MARK.lower() in cmd.lower():
        return "a parameter supplies part of the command at run time"
    if not ALLOW_CHARS_RE.fullmatch(cmd):
        return "uses shell syntax (quoting, variables, redirection, chaining, globs or escapes)"
    words = cmd.split()
    program, args = words[0], words[1:]
    check = ALLOWED_PROGRAMS.get(program)
    if check is None:
        return "runs a program that is not on the auto-publish list"
    for t in args:
        if not t.startswith("-") and not ALLOW_OPERAND_RE.fullmatch(t):
            return "an operand with characters outside the allowed set"
    return check(args)


# --- per-step rules for non-shell tools ----------------------------------------------------

SENSITIVE_PATH_RE = re.compile(r"(?:~|\$\{?home\}?|/users/[^/]+)/(?:\.|library\b)|^/(?:system|library|usr|bin|sbin|etc|private|var|applications|opt|cores)\b|^/$")
TERMINAL_APPS = re.compile(r"\b(?:terminal|iterm2?|warp|alacritty|kitty|wezterm|hyper|ghostty|tabby|script editor|automator|shortcuts|system settings|system preferences|keychain access|xcode|visual studio code|code|cursor)\b")


def _path_mode(spec) -> str:
    return spec.get("mode", "") if isinstance(spec, dict) else str(spec)


# Tools whose text is only shown to the user (a banner, a timer's label and
# spoken line): nothing in it can be pasted, typed, opened or run.
DISPLAY_ONLY_TEXT = {"show_notification", "set_timer"}


def _program_index(toks: list) -> int:
    """Index of a simple command's program word, found the way command_words()
    finds it: past assignments, flags, wrappers and fd numbers."""
    i = 0
    while i < len(toks):
        t = toks[i]
        if re.fullmatch(r"[a-z_][a-z0-9_]*=\S*", t) or t.startswith("-") or t in WRAPPERS or re.fullmatch(r"\d+", t):
            i += 1
            continue
        break
    return i


def _bare_param_receives_pipe(text: str) -> bool:
    """True if a simple command right after a single "|" has a parameter as its
    program word ("cat notes.txt | {x}", "echo <b64> | base64 -d | {x}"): the
    user's value would become the program that reads the piped text. Also what a
    markdown table row ("| {a} | {b} |") looks like, so callers make it a
    needs_review finding, never a block."""
    view = shell_views(text)["dequoted"]
    prev_sep, pos = None, 0
    for m in list(SEP_RE.finditer(view)) + [None]:
        seg = view[pos: m.start()] if m else view[pos:]
        toks = seg.split()
        i = _program_index(toks)
        if prev_sep == "|" and i < len(toks) and toks[i].startswith(PARAM_MARK.lower()):
            return True
        if m is None:
            break
        prev_sep, pos = m.group(0), m.end()
    return False


def _param_only_commands(text: str) -> bool:
    """True if every simple command whose program word is a parameter is that
    parameter alone ("{x}", "- {x}", "{x}\n{y}"). "{x} -rf ~" is not: the author
    wrote the arguments, and the parameter picks the program. Nor is
    "env {x} -rf ~" or "A=1 {x}": the program word is found the way
    command_words() finds it (past wrappers, assignments and flags). Only a
    list-marker "-" may come before a bare parameter -- and not before one with
    arguments: "-" is a zsh precommand modifier, so "- {x} -rf ~" runs {x}."""
    for seg in SEP_RE.split(shell_views(text)["dequoted"]):
        toks = seg.split()
        i = _program_index(toks)
        if i < len(toks) and toks[i].startswith(PARAM_MARK.lower()):
            if len(toks) > i + 1 or any(t != "-" for t in toks[:i]):
                return False
    return True


def analyze_step(i: int, step: dict, tool: dict, variants: list, F: _Findings, facts: dict):
    name = step.get("tool")
    args = step.get("args") if isinstance(step.get("args"), dict) else {}
    base = f"steps[{i}]"
    paths = (tool or {}).get("paths") or {}
    outbound = (tool or {}).get("outbound")

    def each_value(arg):
        seen = set()
        raw = args.get(arg)
        if not isinstance(raw, str):
            return [] if raw is None else [str(raw)]
        out = []
        for values in variants:
            v = substitute(raw, values)
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    if name == "run_command":
        for v in each_value("cmd"):
            why = allowlist_problem(v)
            if why:
                F.add(f"{base}.args.cmd", "not_allowlisted", REVIEW,
                      f"a person must read this command before it is published: {why}")
            f = analyze_shell(v, f"{base}.args.cmd", F)
            for k in ("fetch", "writes", "exec_file", "deletes", "network"):
                facts[k] = facts[k] or f[k]
            facts["cmd_programs"] |= f["programs"]
            facts["cmd_words"] |= {basename(w) for w in command_words(shell_views(v)["deobfuscated"])}
        return

    for arg, spec in paths.items():
        mode = _path_mode(spec)
        for v in each_value(arg):
            p = strip_hidden(v).strip().lower()
            where = f"{base}.args.{arg}"
            if SECRET_RE.search(p):
                F.add(where, "secret_path", BLOCK, "points at credentials or other secrets")
            if LEUTHERIA_STATE_RE.search(p):
                F.add(where, "agent_tamper", BLOCK, "points at Leutheria's own state or settings")
            if mode in ("write", "remove", "move_into", "archive"):
                if PERSIST_PATH_RE.search(p) or (HOME_DOT_RE.search(p) and mode != "archive") or HOME_LIBRARY_RE.search(p):
                    F.add(where, "persistence_path", BLOCK, "writes or removes in ~/Library, a hidden home folder or a startup location")
                elif SENSITIVE_PATH_RE.search(p):
                    F.add(where, "system_path", BLOCK, "writes or removes in a system location")
            elif mode in ("read", "inspect", "copy_from") and (HOME_LIBRARY_RE.search(p) or HOME_DOT_RE.search(p)):
                F.add(where, "private_read", REVIEW, "reads inside ~/Library or a hidden home folder")
            if mode in ("remove", "move_into") and _is_broad(p):
                F.add(where, "mass_move", BLOCK, "removes or moves a whole top-level folder")
            if mode == "write" and EXEC_EXT_RE.search(p):
                facts["writes_exec"] = True
                F.add(where, "writes_executable", REVIEW, "creates a script, app or launch file")
            if p.startswith(PARAM_MARK.lower()):
                F.add(where, "param_path", INFO, "path comes from a parameter (judged by path policy at run time)")
        if mode in ("write", "remove", "move_into"):
            facts["writes"] = True
        if mode == "remove":
            facts["deletes"] = True
        if name in ("open_file", "open_with") and mode == "inspect":
            facts["opens_file"] = True

    if outbound:
        facts["network"] = True
        for v in each_value(outbound):
            where = f"{base}.args.{outbound}"
            u = strip_hidden(v).strip()
            ul = u.lower()
            try:
                parts = urlsplit(u)
                host = (parts.hostname or "").lower()
                scheme = parts.scheme.lower()
            except ValueError:
                host, scheme = "", ""
                F.add(where, "bad_url", BLOCK, "URL does not parse")
            if scheme not in ("http", "https") and not ul.startswith(PARAM_MARK.lower()):
                F.add(where, "url_scheme", BLOCK, "URL is not http(s) (custom schemes and file: URLs launch things)")
            if PARAM_MARK.lower() in host or ul.startswith(PARAM_MARK.lower()):
                F.add(where, "param_host", REVIEW, "a parameter chooses which site is contacted")
            elif host:
                facts["hosts"].add(host)
            if host and re.fullmatch(r"[0-9.]+|0x[0-9a-f]+|[0-9a-f]*:[0-9a-f:.]*", host):
                F.add(where, "ip_host", REVIEW, "contacts a bare IP address")
            if EXFIL_HOST_RE.search(ul):
                F.add(where, "exfil_host", BLOCK, "contacts a request-capture, tunnel or paste service (data exfiltration sink)")
            if SHORTENER_RE.search(host + "/" + (parts.path if host else "")):
                F.add(where, "url_shortener", REVIEW, "uses a URL shortener that hides the destination")
            if "@" in (parts.netloc if host else ""):
                F.add(where, "url_userinfo", REVIEW, "URL contains user info before the host (a classic disguise)")
            if (EXEC_EXT_RE.search(parts.path.lower()) or ARCHIVE_EXT_RE.search(parts.path.lower())) if host else False:
                facts["downloads"] = True
                F.add(where, "download_url", REVIEW, "opens a download (archive, installer or script)")
            if LEUTHERIA_STATE_RE.search(ul) or re.search(r"^(?:https?://)?(?:localhost|127\.|0\.0\.0\.0|\[::1\])", ul):
                F.add(where, "local_url", BLOCK, "contacts this Mac's own services")
            if re.search(r"[?&#][^=]*=" + PARAM_MARK.lower(), ul):
                F.add(where, "param_in_query", INFO, "a parameter value is sent to the site")

    text_args = {
        "write_new_file": ("content",), "append_text": ("text",), "set_clipboard": ("text",), "type_text": ("text",),
        "press_keys": ("keys",), "show_notification": ("title", "message"), "set_timer": ("label", "message"),
    }.get(name, ())
    for arg in text_args:
        for v in each_value(arg):
            where = f"{base}.args.{arg}"
            sub = _Findings()
            analyze_shell(v, where, sub)
            # param_is_program is a rule about commands. In a text that is only shown
            # (DISPLAY_ONLY_TEXT) it never applies. In one that is written, typed or
            # copied it applies unless every parameter-led command is the bare
            # parameter: "{text}" is the user's value, but "{tool} -rf ~" is an
            # author-written command whose program is left to fill in.
            exempt = name in DISPLAY_ONLY_TEXT or _param_only_commands(v)
            serious = [f for f in sub.items if f["severity"] == BLOCK
                       and not (f["rule"] == "param_is_program" and exempt)]
            if serious:
                facts["staged_payload"] = True
                sev = BLOCK if name in ("type_text", "set_clipboard") else REVIEW
                F.add(where, "dangerous_text", sev,
                      "text this step writes, types or copies contains a dangerous shell command" +
                      (" (it would be typed or pasted, outside any confirmation)" if sev == BLOCK else ""))
                facts["staged_rules"] |= {f["rule"] for f in serious}
            elif exempt and name not in DISPLAY_ONLY_TEXT and _bare_param_receives_pipe(v):
                # A bare parameter is the user's value -- unless something is piped
                # into it, when it is the program that reads the pipe. Review, not
                # block: a markdown table row has the same shape.
                F.add(where, "param_receives_pipe", REVIEW,
                      "text this step writes, types or copies pipes into a command whose program a parameter names")
            if name in ("show_notification", "set_timer") and re.search(
                    r"\b(?:say|answer|reply)\s+(?:yes|confirm)|\bconfirm\b|\bapprove\b|\bpassword\b|\benter your\b|\bclick (?:allow|ok)\b", v.lower()):
                F.add(where, "fake_prompt", REVIEW, "a notification or reminder reads like a confirmation or password prompt")
            if name in ("show_notification", "set_timer"):
                analyze_prose(v, where, F)

    if name in ("type_text", "press_keys"):
        F.add(base, "keyboard_control", REVIEW, "types or presses keys in another app (keystrokes aren't confirmed one by one)")
        facts["keyboard"] = True
    if name in ("focus_app", "describe_ui", "open_with", "open_app"):
        for arg in ("name", "app"):
            for v in each_value(arg):
                if TERMINAL_APPS.search(strip_hidden(v).lower()):
                    F.add(f"{base}.args.{arg}", "drives_terminal", BLOCK if name == "focus_app" else REVIEW,
                          "targets a terminal, script editor or settings app")
    if name in ("read_file", "get_clipboard", "search_file_contents", "describe_ui", "take_screenshot", "git_status"):
        facts["reads_private"] = True
    if name == "extract_archive":
        facts["extracts"] = True
    if name == "set_clipboard":
        for v in each_value("text"):
            if re.search(r"(?:^|\s)(?:sudo|curl|wget|bash|sh|zsh|python3?|osascript|rm|chmod)\s", strip_hidden(v).lower()):
                F.add(f"{base}.args.text", "pastejacking", REVIEW, "puts a shell command on the clipboard")


# --- description <-> steps ------------------------------------------------------------------

READONLY_CLAIM = re.compile(
    r"\bread[- ]only\b|\b(?:does ?n[o']t|does not|will not|won'?t|never|cannot|can'?t)\s+(?:change|modify|write|delete|remove|touch|alter|run|execute|install|upload|send)\b"
    r"|\bno (?:changes|side effects|commands|network)\b|\bwithout (?:changing|modifying|deleting|touching)\b|\b(?:only|just|simply) (?:shows?|lists?|reads?|displays?|checks?|reports?|tells?|counts?|looks?)\b"
    r"|\bharmless\b|\bnon-?destructive\b|\bsafe to run\b|\b100% safe\b|\bcompletely safe\b|\bside-effect free\b"
)
OFFLINE_CLAIM = re.compile(r"\boffline\b|\blocal(?:ly)? only\b|\bno (?:internet|network|data leaves)\b|\bnever (?:connects|uploads|sends)\b|\bprivate\b.{0,20}\bnothing (?:is )?sent\b|\bstays on your mac\b")
VOCAB = {
    "delete": r"delet|remov|trash|clean|clear|purg|tidy|empty|wipe|prune|uninstall|reset|discard|erase|free (?:up )?space",
    "write": r"creat|writ|sav|make|new|add|append|copy|cop(?:y|ies)|mov|zip|archiv|extract|unzip|note|log|screenshot|folder|file|backup|back up|export|organi[sz]|journal|record|store|generat|build|install|update|upgrad|set ?up|setup|init|download|clone|pull|sync|commit|snapshot",
    "network": r"download|fetch|internet|web|online|url|link|site|page|http|upload|sync|pull|push|clone|update|upgrad|install|api|server|remote|cloud|weather|news|feed|check for|github|gitlab|brew|npm|pip|search the",
    "command": r"command|shell|terminal|script|run|execut|git|brew|npm|pip|make|build|install|update|upgrad|compil|test|deploy|cli",
    "keyboard": r"type|typing|keys?|keystroke|press|shortcut|enter text|fill",
}
BRANDS = ("github", "gitlab", "google", "gmail", "youtube", "wikipedia", "reddit", "twitter", "apple", "icloud", "dropbox",
          "slack", "notion", "figma", "stackoverflow", "bitbucket", "openai", "anthropic", "claude", "microsoft", "outlook", "amazon", "netflix", "spotify")


def _registrable(host: str) -> str:
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "ac", "gov") and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def analyze_mismatch(skill: dict, facts: dict, F: _Findings):
    prose = " ".join([str(skill.get("name", "")).replace("_", " "), str(skill.get("description", ""))] +
                     [str((s or {}).get("description", "")) for s in ((skill.get("input_schema") or {}).get("properties") or {}).values() if isinstance(s, dict)])
    p = unicodedata.normalize("NFKC", strip_hidden(prose)).lower()
    caps = {
        "delete": facts["deletes"],
        "write": facts["writes"],
        "network": facts["network"],
        "command": facts["has_run_command"],
        "keyboard": facts["keyboard"],
    }
    if READONLY_CLAIM.search(p) and (caps["delete"] or caps["write"] or caps["command"] or caps["keyboard"]):
        F.add("description", "claims_read_only", REVIEW, "description claims the skill only reads or is harmless, but its steps change things or run commands")
    if OFFLINE_CLAIM.search(p) and caps["network"]:
        F.add("description", "claims_offline", REVIEW, "description claims nothing leaves the Mac, but a step contacts the network")
    for cap, present in caps.items():
        if present and not re.search(VOCAB[cap], p):
            F.add("description", f"undisclosed_{cap}", REVIEW, f"steps {'run a shell command' if cap == 'command' else 'use ' + cap + ' capability'} the description never mentions")
    if facts["has_run_command"]:
        words = {w for w in facts["cmd_words"] if re.fullmatch(r"[a-z][a-z0-9._+-]{1,30}", w) and not w.startswith(PARAM_MARK.lower())}
        words -= {"cd", "echo", "printf", "true", "false", "test", "[", "set", "export", "sleep", "cat", "ls", "pwd", "wc", "head", "tail", "grep", "sort", "uniq", "date", "mkdir", "then", "fi"}
        if words and not any(re.search(r"\b" + re.escape(w) + r"\b", p) for w in words) and not re.search(r"\bcommands?\b|\bshell\b|\bterminal\b|\bscripts?\b", p):
            F.add("description", "unnamed_program", REVIEW, "the description names neither the program the command runs nor that it runs a shell command")
    hosts = facts["hosts"]
    if hosts:
        domains_mentioned = set(re.findall(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|dev|app|co|ai|me|edu|gov|so|sh|xyz|info|uk|de|fr|in))\b", p))
        regs = {_registrable(h) for h in hosts}
        if domains_mentioned and not any(_registrable(d) in regs for d in domains_mentioned):
            F.add("description", "site_mismatch", REVIEW, "the description names one site but the steps contact another")
        for b in BRANDS:
            if re.search(r"\b" + b + r"\b", p) and not any(b in r for r in regs):
                F.add("description", "brand_mismatch", REVIEW, "the description names a well-known site, but the steps contact a different domain")
                break


# --- top level -------------------------------------------------------------------------------

def load_catalog_tools(catalog: dict) -> dict:
    tools = catalog.get("tools", catalog) if isinstance(catalog, dict) else {}
    if isinstance(tools, list):
        tools = {t["name"]: t for t in tools if isinstance(t, dict) and "name" in t}
    if not isinstance(tools, dict) or not tools:
        raise ValueError("tool catalog has no tools")
    return tools


def analyze(skill, catalog: dict) -> dict:
    """Run every static rule. Returns {"findings", "verdict_floor", "stats", ...}."""
    tools = load_catalog_tools(catalog)
    F = _Findings()
    facts = {
        "fetch": False, "writes": False, "exec_file": False, "deletes": False, "network": False, "keyboard": False,
        "reads_private": False, "writes_exec": False, "opens_file": False, "downloads": False, "extracts": False,
        "staged_payload": False, "has_run_command": False, "hosts": set(), "cmd_programs": set(), "cmd_words": set(),
        "staged_rules": set(),
    }

    if not isinstance(skill, dict):
        F.add("skill", "structure", BLOCK, "skill file is not a JSON object")
        return _result(F, facts, [])
    steps = skill.get("steps")
    if not isinstance(steps, list) or not steps:
        F.add("steps", "structure", BLOCK, "steps must be a non-empty list")
        steps = []
    name = skill.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
        F.add("name", "structure", BLOCK, "invalid skill name")
    elif name in tools:
        F.add("name", "shadows_tool", BLOCK, "skill name is the name of a built-in tool (it would shadow it)")
    if "source" in skill:
        F.add("source", "structure", BLOCK, "registry files must not carry a source field")
    schema = skill.get("input_schema") if isinstance(skill.get("input_schema"), dict) else {}
    variants = param_variants(schema)

    used = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
            F.add(f"steps[{i}]", "structure", BLOCK, "step must be {tool, args}")
            continue
        tname = step["tool"]
        if tname not in tools:
            F.add(f"steps[{i}].tool", "unknown_tool", BLOCK, "calls something that is not a registered tool")
            continue
        used.append(tname)
        if tname == "run_command":
            facts["has_run_command"] = True
        if tools[tname].get("safety") == "scoped" and tname not in ("type_text", "press_keys"):
            F.add(f"steps[{i}]", "gui_control", REVIEW, "uses GUI control")
        analyze_step(i, step, tools[tname], variants, F, facts)

    # prose the end user's model will see
    analyze_prose(str(skill.get("description", "")), "description", F)
    analyze_prose(str(name or "").replace("_", " "), "name", F)
    for pname, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, dict):
            continue
        analyze_prose(str(pname).replace("_", " "), f"input_schema.properties.{pname}", F)
        analyze_prose(str(spec.get("description", "")), f"input_schema.properties.{pname}.description", F)
        for k in ("enum", "default"):
            vals = spec.get(k)
            for v in (vals if isinstance(vals, list) else [vals] if vals is not None else []):
                if isinstance(v, str):
                    analyze_prose(v, f"input_schema.properties.{pname}.{k}", F)

    # cross-step chains (each step alone may look fine)
    if facts["staged_payload"] and (facts["has_run_command"] or facts["opens_file"] or facts["keyboard"]):
        F.add("steps", "staged_execution", BLOCK, "writes a dangerous command to a file or the clipboard and has a step that can run, open or type it")
    if facts["writes_exec"] and (facts["has_run_command"] or facts["opens_file"]):
        F.add("steps", "write_then_execute", BLOCK if facts["staged_payload"] else REVIEW,
              "creates a script or app file and has a step that can run or open it")
    if facts["downloads"] and (facts["extracts"] or facts["opens_file"] or facts["has_run_command"]):
        F.add("steps", "download_chain", BLOCK, "opens a download and then extracts, opens or runs files (review chain D)")
    if facts["fetch"] and facts["exec_file"]:
        F.add("steps", "download_and_execute", BLOCK, "downloads in one place and runs or opens files in another")
    if facts["reads_private"] and facts["network"]:
        F.add("steps", "read_then_send", REVIEW, "reads local or on-screen content and contacts the network in the same skill")
    if facts["keyboard"] and facts["has_run_command"]:
        F.add("steps", "keyboard_and_shell", REVIEW, "combines typing into apps with shell commands")

    analyze_mismatch(skill, facts, F)
    return _result(F, facts, used)


def _result(F: _Findings, facts: dict, used: list) -> dict:
    sev = worst(f["severity"] for f in F.items)
    return {
        "analyzer_version": ANALYZER_VERSION,
        "max_severity": sev,
        "findings": sorted(F.items, key=lambda f: (-_RANK[f["severity"]], f["where"], f["rule"])),
        "stats": {
            "tools": sorted(set(used)),
            "step_count": len(used),
            "has_run_command": facts["has_run_command"],
            "contacts_network": facts["network"],
            "writes": facts["writes"],
            "deletes": facts["deletes"],
            "keyboard": facts["keyboard"],
            "hosts": sorted(facts["hosts"]),
        },
    }
