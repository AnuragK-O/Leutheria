import re

# The whole placeholder grammar for generated skills, in one place (the
# validator imports it from here so the two can never disagree):
#   {param}   replaced by the value of `param`
#   {{  }}    a literal brace (same escape str.format used, so older files
#             written under str.format read the same)
#   anything else with braces -- "{}", "{print $1}", a lone "{" -- is literal
#
# This used to be str.format(**args), which (BUGS.md #24) gave up on the
# *whole string* at the first brace it couldn't resolve -- `find {dir} -exec
# ls {} \;` ran with a literal "{dir}" -- raised on a stray "{", and handed
# every template the format mini-language ("{p.__class__}", "{p:>999999999}").
# Substitution is a single left-to-right pass, so a value is inserted once
# and never re-scanned: a value containing "{other}" stays literal.
PARAM_PATTERN = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
TOKEN_RE = re.compile(r"\{\{|\}\}|\{(" + PARAM_PATTERN + r")\}")


def placeholders(value) -> set:
    """Every {param} name referenced anywhere inside a step's args."""
    if isinstance(value, str):
        return {m.group(1) for m in TOKEN_RE.finditer(value) if m.group(1)}
    if isinstance(value, dict):
        return set().union(*(placeholders(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(placeholders(v) for v in value)) if value else set()
    return set()


def escape_literal(text: str) -> str:
    """Make a literal safe to bake into a template: its braces can no longer
    be read as placeholders."""
    return text.replace("{", "{{").replace("}", "}}")


def _substitute(value, args: dict):
    if isinstance(value, str):
        whole = TOKEN_RE.fullmatch(value)
        if whole and whole.group(1):
            # The entire string is one placeholder: pass the value through
            # with its JSON type intact (a number stays a number).
            return args[whole.group(1)]

        def _token(m):
            if m.group(1):
                return str(args[m.group(1)])
            return m.group(0)[0]  # "{{" -> "{", "}}" -> "}"

        return TOKEN_RE.sub(_token, value)
    if isinstance(value, dict):
        return {k: _substitute(v, args) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, args) for v in value]
    return value


def _resolve_args(definition: dict, args: dict) -> tuple:
    """The values every placeholder will get, or an error naming what's
    missing. Checked before any step runs, so a skill never half-executes
    and then trips over a parameter it was never given."""
    properties = definition.get("input_schema", {}).get("properties", {})
    resolved = dict(args or {})
    for name, spec in properties.items():
        if name not in resolved and isinstance(spec, dict) and "default" in spec:
            resolved[name] = spec["default"]
    used = set().union(*(placeholders(step["args"]) for step in definition["steps"]))
    missing = sorted(used - resolved.keys())
    if missing:
        return None, f"missing value for parameter(s): {', '.join(missing)}"
    return resolved, None


async def run_template(definition: dict, args: dict, websocket, pending: dict) -> dict:
    """Execute a generated skill: a plain ordered list of {tool, args} steps
    with {param} placeholders. This is the ONE reviewed interpreter every
    auto-generated skill runs through -- a generated skill is data, not new
    code, so it's structurally incapable of doing anything a hand-written
    skill couldn't. Each step still goes through dispatch(), so a destructive
    step still pauses for its own confirmation, same as always.
    """
    from agent.core.dispatcher import dispatch
    from agent.skills.registry import SKILLS
    from agent.tools.registry import TOOLS

    # Already enforced by skills/validate.py on load and register; checked
    # again here because this is the one place every generated step passes.
    not_tools = [s["tool"] for s in definition["steps"] if s["tool"] not in TOOLS or s["tool"] in SKILLS]
    if not_tools:
        return {"ok": False, "error": f"generated skills may only call tools, not {not_tools}", "steps": []}

    values, error = _resolve_args(definition, args)
    if error:
        return {"ok": False, "error": error, "steps": []}

    steps_log = []
    for step in definition["steps"]:
        step_args = _substitute(step["args"], values)
        result = await dispatch(step["tool"], step_args, websocket, pending)
        steps_log.append({"tool": step["tool"], "args": step_args, "result": result})
        if not (isinstance(result, dict) and result.get("ok")):
            return {
                "ok": False,
                "error": f"step '{step['tool']}' failed or was declined",
                "steps": steps_log,
            }

    return {
        "ok": True,
        "message": f"Ran generated skill '{definition['name']}' successfully.",
        "steps": steps_log,
    }
