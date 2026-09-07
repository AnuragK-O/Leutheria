import json

from agent.core.logging_util import LOG_FILE
from agent.skills.registry import SKILLS

NOT_APPLICABLE = object()  # sentinel: "don't consider a proposal at all" (distinct from
                            # None, which means "consider it, but no past example exists")


def _signature(trace: list) -> tuple:
    return tuple(step["tool"] for step in trace)


def _load_historical_traces() -> list:
    """Every past successful multi-step trace (trace length >= 2), oldest first."""
    if not LOG_FILE.exists():
        return []

    traces = []
    with LOG_FILE.open() as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "message_out":
                continue
            response = record.get("response", {})
            trace = response.get("trace")
            if response.get("type") == "response" and trace and len(trace) >= 2:
                traces.append(trace)
    return traces


def find_matching_past_trace(trace: list):
    """Most recent past trace with the same ordered tool-name sequence, or None
    if this is the first time this sequence has been seen."""
    sig = _signature(trace)
    matches = [t for t in _load_historical_traces() if _signature(t) == sig]
    return matches[-1] if matches else None


def check_before_logging(response: dict):
    """Convenience wrapper for the server: given a just-produced response,
    decide whether a skill proposal should be considered at all. Returns:
      - NOT_APPLICABLE if no proposal should happen (not a multi-step
        success, already covered by an existing skill, or previously
        declined for this exact tool sequence)
      - a past matching trace if this is a confirmed repeat (confident,
        two-example generation)
      - None if this is the first time this sequence has been seen (still
        worth proposing, but single-example generation with clarifying
        questions instead of a confident diff)

    MUST be called before the caller logs this response's own message_out
    event -- otherwise the scan below would see this request's own entry
    already on disk and match against itself.
    """
    if response.get("type") != "response":
        return NOT_APPLICABLE
    trace = response.get("trace") or []
    if len(trace) < 2 or has_matching_skill(trace) or has_declined_signature(trace):
        return NOT_APPLICABLE
    return find_matching_past_trace(trace)


def has_matching_skill(trace: list) -> bool:
    """True if a skill (hand-written or previously generated) already covers
    this exact tool-name sequence -- avoids re-proposing something that's
    already been turned into a skill."""
    sig = list(_signature(trace))
    return any(skill.get("source_signature") == sig for skill in SKILLS.values())


def has_declined_signature(trace: list) -> bool:
    """True if a proposal for this exact tool sequence was declined before --
    keyed by signature (not name, which can vary per generation attempt) so
    it holds regardless of what the model happened to call it each time."""
    if not LOG_FILE.exists():
        return False

    sig = list(_signature(trace))
    with LOG_FILE.open() as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "skill_declined" and record.get("source_signature") == sig:
                return True
    return False


def _extract_json(text: str):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _summarize(trace: list) -> str:
    return json.dumps([{"tool": s["tool"], "args": s["args"]} for s in trace], indent=2)


def generate_skill_definition(backend, trace: list, past_trace: list):
    """Diff two concrete runs of the same tool sequence into a reusable,
    parameterized skill definition. Confident: two data points make it
    possible to see what actually varied vs. what stayed constant, so there
    are no clarifying questions. Returns None if generation fails --
    callers should just skip the proposal in that case, not error out."""

    prompt = f"""You'll see two examples of the same kind of multi-step task, run at
different times with different concrete values. Generalize them into a reusable skill
template.

Example 1:
{_summarize(past_trace)}

Example 2:
{_summarize(trace)}

Respond with ONLY a JSON object (no prose, no markdown fences) with this exact shape:
{{
  "name": "short_snake_case_name",
  "description": "One sentence a router would use to decide when to invoke this skill.",
  "input_schema": {{
    "type": "object",
    "properties": {{ "<param>": {{"type": "string", "description": "..."}} }},
    "required": ["<param>", "..."]
  }},
  "steps": [
    {{"tool": "<tool name>", "args": {{"<arg name>": "<value, with {{param}} placeholders for anything that varied between the two examples>"}}}}
  ]
}}

Compare the two examples to figure out which argument values are constant (keep them
literal) versus which varied (replace with a {{param_name}} placeholder and add that
param to input_schema). Use the exact tool names and argument names from the examples."""

    turn = backend.generate([{"role": "user", "content": prompt}], [])
    definition = _extract_json(turn.text)
    if definition is None:
        return None

    definition.setdefault("uncertain_params", [])
    definition["source_signature"] = list(_signature(trace))
    return definition


def generate_skill_definition_single(backend, trace: list, user_text: str = ""):
    """Propose a skill from a single successful run -- the first time this
    tool sequence has ever been seen. Since there's no second example to
    diff against, any argument value that *plausibly* varies between runs
    (rather than obviously being a one-off constant) gets flagged under
    "uncertain_params" instead of being silently guessed either way -- the
    caller surfaces these as yes/no questions before the skill is saved.
    """

    prompt = f"""A user just completed this multi-step task successfully. Propose a
reusable skill template for it, even though you've only seen it run once.

Original request: {user_text or "(not available)"}

Concrete steps that ran:
{_summarize(trace)}

Respond with ONLY a JSON object (no prose, no markdown fences) with this exact shape:
{{
  "name": "short_snake_case_name",
  "description": "One sentence a router would use to decide when to invoke this skill.",
  "input_schema": {{
    "type": "object",
    "properties": {{ "<param>": {{"type": "string", "description": "..."}} }},
    "required": ["<param>", "..."]
  }},
  "steps": [
    {{"tool": "<tool name>", "args": {{"<arg name>": "<value, with {{param}} placeholders for anything you're confident would vary each time>"}}}}
  ],
  "uncertain_params": [
    {{"name": "<param>", "guessed_value": "<the literal value seen in this one example>",
      "question": "A short question asking the user whether this actually varies between runs or should always stay fixed at the guessed value."}}
  ]
}}

Only list a param under "uncertain_params" if you're genuinely unsure whether it should
vary or stay constant. A specific name the user explicitly mentioned in their request
(like a project name) is clearly a real parameter -- put it straight into "steps" and
"input_schema", not "uncertain_params". Something like a location that only appeared
once (e.g. "~/Desktop") is a good candidate for "uncertain_params", since a single
example can't tell you whether the user always means that exact place."""

    turn = backend.generate([{"role": "user", "content": prompt}], [])
    definition = _extract_json(turn.text)
    if definition is None:
        return None

    definition.setdefault("uncertain_params", [])
    definition["source_signature"] = list(_signature(trace))
    return definition


def finalize_definition(definition: dict, resolutions: dict) -> dict:
    """Apply the user's answers to any uncertain_params before saving. For
    each one where the user said "always this value" (resolutions[name] is
    False), bake the guessed literal back into every step and drop it from
    input_schema; otherwise leave it as a real parameter. Unanswered
    params default to staying a parameter (fail toward flexibility, not
    toward silently hardcoding something that might actually vary).
    """
    definition = json.loads(json.dumps(definition))  # deep copy
    uncertain = definition.pop("uncertain_params", [])

    def _bake(value, name, literal):
        if isinstance(value, str):
            return value.replace("{" + name + "}", literal)
        if isinstance(value, dict):
            return {k: _bake(v, name, literal) for k, v in value.items()}
        return value

    for item in uncertain:
        name = item["name"]
        if resolutions.get(name, True):  # True = "it varies" = keep as a param
            continue
        literal = item["guessed_value"]
        for step in definition["steps"]:
            step["args"] = _bake(step["args"], name, literal)
        definition["input_schema"]["properties"].pop(name, None)
        if name in definition["input_schema"].get("required", []):
            definition["input_schema"]["required"].remove(name)

    return definition
