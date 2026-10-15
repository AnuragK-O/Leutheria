"""Text rules for shared skills: what the model is told about an imported
skill, and what a skill being shared is scrubbed of. Pure functions with
no agent state, so they can be tested (and reused by the registry's CI)
on their own.

Two directions:

  in   A shared skill's description is prompt text sent with EVERY request
       (skills are tools to the model), so a stranger's prose is never sent
       verbatim. model_description() builds the model-facing text locally
       from the index's summary, the author's login and the tools the steps
       call; sanitize_prose() is what gets to survive of the summary and of
       each parameter description:
         - Unicode normalised (NFKC), control/format characters (zero-width,
           bidi overrides) removed;
         - URLs and bare domain names removed;
         - markdown and markup characters removed;
         - any sentence with instruction-like phrasing dropped whole
           (INSTRUCTION_RE: "ignore previous...", "you must", "always call",
           "system:", "do not tell", "instead of", "important:", ...);
         - capped, at a word boundary.
       This is a filter, not a proof: it narrows what a hostile summary can
       say to a short plain sentence, next to a description the agent
       wrote. The registry's screener and the confirmation rules are the
       other layers.

  out  share_sanitize() turns a learned skill into a registry file: the
       user's home folder becomes "~", other people's home paths and
       personal-looking constants (emails, phone numbers, addresses, URLs,
       tokens, a folder deeper than a standard one) are flagged, and any
       flag the user picks becomes a parameter -- the uncertain-params idea
       from skill learning, run in reverse.
"""

import copy
import difflib
import json
import re
import unicodedata

from agent.skills.template_skill import PARAM_PATTERN, TOKEN_RE

SUMMARY_MAX = 200
PARAM_DESCRIPTION_MAX = 150
AUTHOR_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")

INSTRUCTION_RE = re.compile(
    r"\b(ignore|disregard|forget|override|bypass|skip)\b.{0,40}\b(instructions?|previous|prior|above|earlier|rules?|"
    r"prompts?|guidelines?|system|confirmations?|safety|policy|policies)\b"
    r"|\byou\s+(must|should|shall|will|are|need\s+to|have\s+to|can\s+now)\b"
    r"|\b(always|never|only)\s+(call|use|run|invoke|ask|tell|confirm|trust|approve|prefer|choose|pick|select|say|reply|respond|answer)\b"
    r"|\bsystem\s*prompt\b|\b(assistant|system|user|human|developer)\s*:"
    r"|\b(do\s+not|don'?t|never)\s+(tell|ask|mention|confirm|warn|inform|show)\b"
    r"|\b(pretend|act\s+as|role\s*-?\s*play|jailbreak|developer\s+mode)\b"
    r"|\bnew\s+instructions?\b|\binstead\s+of\b|\bimportant\s*[:!]|\bbefore\s+(any|every|all)\s+other\b"
    r"|\b(the|this)\s+(model|assistant|ai|llm|agent)\b|\btool\s+(list|call|use)\b|\bprompt\b",
    re.IGNORECASE | re.DOTALL,
)
_URL_RE = re.compile(r"\b(?:[a-z][a-z0-9+.-]*://|www\.)\S*", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.(?:[a-z]{2,24})\b(?:/\S*)?", re.IGNORECASE)
_MARKUP_RE = re.compile(r"[*_`#>|~\[\]{}<>\\^=$@]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")


def _visible(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch if not unicodedata.category(ch).startswith("C") else " " for ch in text.replace("\n", " \n "))


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2 :]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def sanitize_prose(text, limit: int = SUMMARY_MAX) -> str:
    """See the module docstring. Returns "" when nothing acceptable is left."""
    if not isinstance(text, str):
        return ""
    text = _visible(text)
    text = _URL_RE.sub(" ", text)
    text = _DOMAIN_RE.sub(" ", text)
    text = _MARKUP_RE.sub(" ", text)
    kept = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        sentence = re.sub(r"\s+", " ", sentence).strip(" -")
        if sentence and not INSTRUCTION_RE.search(sentence):
            kept.append(sentence)
    return _cap(" ".join(kept).strip(), limit)


def safe_author(author) -> str:
    return author if isinstance(author, str) and AUTHOR_RE.fullmatch(author) else "an unknown author"


def model_description(author, summary, requires: list, has_run_command: bool) -> str:
    """The only description of an imported skill the model ever sees."""
    text = sanitize_prose(summary, SUMMARY_MAX).rstrip(" .") or "no summary"
    tools = ", ".join(t for t in requires if isinstance(t, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", t))
    line = f"Shared skill by {safe_author(author)}: {text}. Runs: {tools}."
    if has_run_command:
        line += " Each shell command in it asks the user before it runs."
    return line


def model_input_schema(schema: dict) -> dict:
    """The skill's input_schema with every parameter description sanitized
    (the author wrote those too). Types, enums, defaults and required are
    behaviour, covered by content_hash, and kept as they are."""
    out = copy.deepcopy(schema)
    for spec in (out.get("properties") or {}).values():
        if isinstance(spec, dict) and "description" in spec:
            cleaned = sanitize_prose(spec["description"], PARAM_DESCRIPTION_MAX)
            if cleaned:
                spec["description"] = cleaned
            else:
                spec.pop("description")
    return out


# --- share (out) ------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<![\w/.-])\+?\d[\d ()-]{7,}\d(?![\w/.-])")
_IP_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_SHARE_URL_RE = re.compile(r"\bhttps?://[^\s\"'{}]+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"(?<![\w/])(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{24,}(?![\w/])")
_OTHER_HOME_RE = re.compile(r"/Users/(?!Shared\b)[^/\s\"'{}]+")
_TILDE_PATH_RE = re.compile(r"~/([^/\s\"'{}]+)/([^\s\"'{}]+)")


def _walk_strings(value, where: str):
    """(where, string) for every string inside a JSON value."""
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{where}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _walk_strings(item, f"{where}[{i}]")


def _map_strings(value, fn):
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: _map_strings(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_strings(v, fn) for v in value]
    return value


def _literal_spans(text: str):
    """The parts of a template string that are literal (not {param})."""
    last = 0
    for m in TOKEN_RE.finditer(text):
        if m.group(1):
            yield text[last : m.start()]
            last = m.end()
    yield text[last:]


def _param_name(base: str, taken: set) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", base).strip("_").lower() or "value"
    if not re.fullmatch(PARAM_PATTERN, base):
        base = "value_" + base
    base = base[:56]
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}_{n}"
    return name


def share_sanitize(definition: dict, homes, username: str, standard_folders, parameterize=()) -> dict:
    """Content keys only in, content keys only out (the caller adds fresh
    metadata). Returns {definition, changes, flags, diff}."""
    content = {k: copy.deepcopy(definition[k]) for k in ("name", "description", "input_schema", "steps")}
    before = copy.deepcopy(content)
    changes = []

    homes = sorted({h.rstrip("/") for h in homes if h and h.rstrip("/")}, key=len, reverse=True)
    home_res = [re.compile(re.escape(h) + r"(?=/|$|[^\w.-])") for h in homes]

    def to_tilde(text: str) -> str:
        for rx in home_res:
            text = rx.sub("~", text)
        return text

    for key in ("description", "input_schema", "steps"):
        content[key] = _map_strings(content[key], to_tilde)
    for (where, old), (_, new) in zip(_walk_strings(before, "skill"), _walk_strings(content, "skill")):
        if old != new:
            changes.append({"where": where, "before": old, "after": new, "why": "your home folder is written as ~"})

    flags = []
    seen = set()

    def flag(where, value, kind, arg_key):
        if (where, value) in seen:
            return
        seen.add((where, value))
        flags.append({"id": f"f{len(flags) + 1}", "where": where, "value": value, "kind": kind, "arg": arg_key})

    std = {f.lower() for f in standard_folders}
    user_re = re.compile(r"(?<![A-Za-z0-9])" + re.escape(username) + r"(?![A-Za-z0-9])", re.IGNORECASE) if username else None
    for i, step in enumerate(content["steps"]):
        for arg_key, value in step["args"].items():
            for where, text in _walk_strings(value, f"steps[{i}].args.{arg_key}"):
                for span in _literal_spans(text):
                    for rx, kind in (
                        (_EMAIL_RE, "email"), (_SHARE_URL_RE, "url"), (_IP_RE, "ip_address"),
                        (_PHONE_RE, "phone_number"), (_TOKEN_RE, "token_or_id"), (_OTHER_HOME_RE, "someone_elses_home"),
                    ):
                        for m in rx.finditer(span):
                            flag(where, m.group(0), kind, arg_key)
                    for m in _TILDE_PATH_RE.finditer(span):
                        if m.group(1).lower() not in std or m.group(2).strip("/"):
                            flag(where, m.group(0).rstrip("/"), "personal_path", arg_key)
                    if user_re and user_re.search(span):
                        flag(where, username, "your_username", arg_key)
    for where, text in _walk_strings(content["description"], "skill.description"):
        for rx, kind in ((_EMAIL_RE, "email"), (_SHARE_URL_RE, "url"), (_PHONE_RE, "phone_number")):
            for m in rx.finditer(text):
                flag(where, m.group(0), kind, None)
        if user_re and user_re.search(text):
            flag(where, username, "your_username", None)

    # Flags the user chose to turn into parameters.
    chosen = {f["id"]: f for f in flags if f["id"] in set(parameterize or ())}
    schema = content["input_schema"]
    schema.setdefault("properties", {})
    taken = set(schema["properties"])
    for f in chosen.values():
        if not f["where"].startswith("steps["):
            continue  # a description can't take a parameter; the flag stays a warning
        name = _param_name({"personal_path": "folder", "your_username": "username"}.get(f["kind"], f["kind"]), taken)
        taken.add(name)
        step_index = int(f["where"][len("steps[") : f["where"].index("]")])
        step = content["steps"][step_index]
        old_args = copy.deepcopy(step["args"])
        escaped = f["value"].replace("{", "{{").replace("}", "}}")
        step["args"][f["arg"]] = _map_strings(step["args"][f["arg"]], lambda s: s.replace(escaped, "{" + name + "}"))
        if step["args"] != old_args:
            schema["properties"][name] = {"type": "string", "description": f"The {f['kind'].replace('_', ' ')} to use."}
            schema.setdefault("required", [])
            schema["required"].append(name)
            f["param"] = name
            changes.append(
                {"where": f["where"], "before": json.dumps(old_args[f["arg"]]), "after": json.dumps(step["args"][f["arg"]]),
                 "why": f"{f['value']} is now the parameter {name}"}
            )
    if not schema.get("required"):
        schema.pop("required", None)

    diff = "".join(
        difflib.unified_diff(
            json.dumps(before, indent=2, ensure_ascii=False).splitlines(keepends=True),
            json.dumps(content, indent=2, ensure_ascii=False).splitlines(keepends=True),
            fromfile="your skill", tofile="shared copy",
        )
    )
    return {"definition": content, "changes": changes, "flags": flags, "diff": diff}
