"""The format contract for generated skills, checked on the way in (register)
and on the way back out (load from disk).

A generated skill is data interpreted by template_skill.run_template(), and
this module is what makes "data" mean something: a flat list of calls to
registered tools, with arguments that are plain JSON, placeholders that all
resolve to declared parameters, and a name that can only ever map to one
file inside generated/. It's written to hold for a file nobody on this
machine wrote -- a future shared skill -- so it rejects rather than repairs.

Metadata (id / content_hash / version / source / requires / created_at) is
what a shared copy will be compared by; see content_hash() for why the
hash covers what it does.
"""

import hashlib
import json
import math
import re
import uuid
from pathlib import Path

from agent.skills.template_skill import PARAM_PATTERN, placeholders

NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
PARAM_RE = re.compile(PARAM_PATTERN)
SOURCE_RE = re.compile(r"learned|imported:[0-9a-f]{64}")
HASH_RE = re.compile(r"[0-9a-f]{64}")

# Names a file can't safely have on some filesystem a shared skill might
# travel to. Lowercase-only names already rule out case-folding collisions.
RESERVED_FILENAMES = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(10)}

CONTENT_KEYS = {"name", "description", "input_schema", "steps"}
METADATA_KEYS = {"id", "content_hash", "version", "source", "requires", "created_at"}
OPTIONAL_KEYS = {"source_signature"}
ALLOWED_KEYS = CONTENT_KEYS | METADATA_KEYS | OPTIONAL_KEYS

PARAM_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool}
PARAM_KEYS = {"type", "description", "enum", "default"}

MAX_DESCRIPTION = 1000
MAX_PARAM_DESCRIPTION = 500
MAX_PARAMS = 20
MAX_STEPS = 50
MAX_STRING = 10_000
MAX_DEPTH = 6

# Bumping this changes every hash on purpose: it's what lets the hash's scope
# be redefined later without two formats ever producing the same digest.
HASH_DOMAIN = "leutheria-skill-content-v1"


class SkillValidationError(ValueError):
    pass


def _fail(message: str):
    raise SkillValidationError(message)


# --- derived metadata --------------------------------------------------------


def _schema_behaviour(input_schema: dict) -> dict:
    """The parts of input_schema that change what the skill *does*: which
    params exist, their types, enums and defaults, and which are required.
    Descriptions are left out -- see content_hash()."""
    properties = {}
    for name, spec in (input_schema.get("properties") or {}).items():
        properties[name] = {k: v for k, v in spec.items() if k != "description"}
    return {"properties": properties, "required": sorted(set(input_schema.get("required") or []))}


def content_hash(definition: dict) -> str:
    """sha256 over canonical JSON of the skill's behaviour: its steps (order
    matters, it's a procedure) and the behaviour-relevant part of its
    input_schema.

    Deliberately excluded:
      - name / description / param descriptions: prose. Two users who learned
        the same procedure and worded it differently should get the same hash,
        which is what a shared database would dedupe and compare on. The
        flip side: the hash says nothing about the prose, so it is not an
        integrity check of the file -- that has to be a separate whole-file
        digest when sharing actually exists.
      - id / version / source / created_at / requires / source_signature:
        metadata about the content, or derived from it.
    Canonical form: sorted keys, no whitespace, UTF-8, prefixed with
    HASH_DOMAIN so a future change of scope can't collide with this one.
    """
    payload = {
        "steps": [{"tool": step["tool"], "args": step["args"]} for step in definition["steps"]],
        "input_schema": _schema_behaviour(definition["input_schema"]),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256((HASH_DOMAIN + "\n" + canonical).encode("utf-8")).hexdigest()


def requires(definition: dict) -> list:
    return sorted({step["tool"] for step in definition["steps"]})


def new_metadata(definition: dict, created_at: float) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "content_hash": content_hash(definition),
        "version": 1,
        "source": "learned",
        "requires": requires(definition),
        "created_at": created_at,
    }


# --- paths -------------------------------------------------------------------


def check_name(name) -> None:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        _fail(f"invalid skill name {name!r}: must match ^[a-z][a-z0-9_]{{0,63}}$")
    if name in RESERVED_FILENAMES:
        _fail(f"invalid skill name {name!r}: reserved filename")


def skill_path(generated_dir: Path, name: str) -> Path:
    """The only way a skill name becomes a path: validated first, then
    checked to still land directly inside generated/ once resolved."""
    check_name(name)
    root = Path(generated_dir).resolve()
    path = (root / f"{name}.json").resolve()
    if path.parent != root:
        _fail(f"skill path for {name!r} escapes {root}")
    return path


# --- structure ---------------------------------------------------------------


def _check_json_value(value, where: str, depth: int = 0) -> None:
    """Plain JSON data only: no NaN/Infinity, no non-string keys, bounded."""
    if depth > MAX_DEPTH:
        _fail(f"{where}: nested deeper than {MAX_DEPTH}")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(f"{where}: non-finite number")
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING:
            _fail(f"{where}: string longer than {MAX_STRING}")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check_json_value(item, f"{where}[{i}]", depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(f"{where}: non-string key {key!r}")
            _check_json_value(item, f"{where}.{key}", depth + 1)
        return
    _fail(f"{where}: {type(value).__name__} is not plain JSON data")


def _check_input_schema(schema) -> None:
    if not isinstance(schema, dict):
        _fail("input_schema must be an object")
    extra = set(schema) - {"type", "properties", "required"}
    if extra:
        _fail(f"input_schema has unsupported keys: {sorted(extra)}")
    if schema.get("type") != "object":
        _fail('input_schema.type must be "object"')
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        _fail("input_schema.properties must be an object")
    if len(properties) > MAX_PARAMS:
        _fail(f"more than {MAX_PARAMS} parameters")
    for name, spec in properties.items():
        if not PARAM_RE.fullmatch(name):
            _fail(f"invalid parameter name {name!r}")
        if not isinstance(spec, dict):
            _fail(f"parameter {name!r} must be an object")
        extra = set(spec) - PARAM_KEYS
        if extra:
            _fail(f"parameter {name!r} has unsupported keys: {sorted(extra)}")
        kind = spec.get("type")
        if kind not in PARAM_TYPES:
            _fail(f"parameter {name!r} type must be one of {sorted(PARAM_TYPES)}")
        py_type = PARAM_TYPES[kind]

        def _typed(v):
            return isinstance(v, py_type) and not (isinstance(v, bool) and kind != "boolean")

        description = spec.get("description", "")
        if not isinstance(description, str) or len(description) > MAX_PARAM_DESCRIPTION:
            _fail(f"parameter {name!r} description must be a string of at most {MAX_PARAM_DESCRIPTION} chars")
        if "enum" in spec:
            enum = spec["enum"]
            if not isinstance(enum, list) or not enum or not all(_typed(v) for v in enum):
                _fail(f"parameter {name!r} enum must be a non-empty list of {kind} values")
            _check_json_value(enum, f"parameter {name!r} enum")
        if "default" in spec:
            if not _typed(spec["default"]):
                _fail(f"parameter {name!r} default must be a {kind}")
            _check_json_value(spec["default"], f"parameter {name!r} default")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        _fail("input_schema.required must be a list of names")
    if len(set(required)) != len(required):
        _fail("input_schema.required has duplicates")
    undeclared = set(required) - set(properties)
    if undeclared:
        _fail(f"required parameter(s) not declared in properties: {sorted(undeclared)}")


def _check_steps(steps, tools: dict, skill_names) -> set:
    """Returns every placeholder name the steps reference."""
    if not isinstance(steps, list) or not steps:
        _fail("steps must be a non-empty list")
    if len(steps) > MAX_STEPS:
        _fail(f"more than {MAX_STEPS} steps")
    used = set()
    for i, step in enumerate(steps):
        where = f"step {i}"
        if not isinstance(step, dict) or set(step) != {"tool", "args"}:
            _fail(f"{where} must be exactly {{tool, args}}")
        tool = step["tool"]
        # Tools only, never another skill: every generated skill stays a flat
        # step list you can read top to bottom, with no recursion and nothing
        # hidden behind some other skill's name.
        if tool in skill_names and tool not in tools:
            _fail(f"{where} calls skill {tool!r}: generated skills may only call tools")
        if tool not in tools:
            _fail(f"{where} calls unknown tool {tool!r}")
        args = step["args"]
        if not isinstance(args, dict):
            _fail(f"{where} args must be an object")
        _check_json_value(args, f"{where} args")
        tool_schema = tools[tool].get("input_schema", {})
        known = set(tool_schema.get("properties", {}))
        unknown = set(args) - known
        if unknown:
            _fail(f"{where}: {tool} has no argument(s) {sorted(unknown)}")
        missing = set(tool_schema.get("required", [])) - set(args)
        if missing:
            _fail(f"{where}: {tool} is missing required argument(s) {sorted(missing)}")
        used |= placeholders(args)
    return used


def validate_content(definition, *, tools: dict, reserved_names=(), skill_names=()) -> None:
    """Everything except metadata: name, description, schema, steps.

    reserved_names: names this skill may not take (every tool and hand-written
    skill -- the dispatcher resolves skills before tools, so a generated skill
    named after a tool would shadow it). skill_names: all skill names, so a
    skill-as-step gets the specific error rather than "unknown tool".
    """
    if not isinstance(definition, dict):
        _fail("definition must be a JSON object")
    missing = CONTENT_KEYS - set(definition)
    if missing:
        _fail(f"missing keys: {sorted(missing)}")
    extra = set(definition) - ALLOWED_KEYS
    if extra:
        _fail(f"unknown keys: {sorted(extra)}")

    name = definition["name"]
    check_name(name)
    if name in reserved_names:
        _fail(f"name {name!r} collides with an existing tool or built-in skill")

    description = definition["description"]
    if not isinstance(description, str) or not description.strip():
        _fail("description must be a non-empty string")
    if len(description) > MAX_DESCRIPTION:
        _fail(f"description longer than {MAX_DESCRIPTION} chars")

    schema = definition["input_schema"]
    _check_input_schema(schema)
    used = _check_steps(definition["steps"], tools, set(skill_names))

    declared = set(schema.get("properties", {}))
    undeclared = used - declared
    if undeclared:
        _fail(f"placeholder(s) not declared in input_schema: {sorted(undeclared)}")
    # A declared parameter no step uses is an interface that lies: the model
    # is told the skill takes it, fills it in, and it does nothing. An error,
    # not a warning -- finalize_definition() already prunes unused *optional*
    # params, so what reaches here unused is a broken generalization.
    unused = declared - used
    if unused:
        _fail(f"declared parameter(s) never used by any step: {sorted(unused)}")

    if "source_signature" in definition:
        sig = definition["source_signature"]
        if sig is not None and (not isinstance(sig, list) or not all(isinstance(s, str) for s in sig)):
            _fail("source_signature must be a list of tool names")


def validate_metadata(definition: dict) -> None:
    """Metadata present, well-formed, and consistent with the content."""
    missing = METADATA_KEYS - set(definition)
    if missing:
        _fail(f"missing metadata: {sorted(missing)}")
    try:
        if str(uuid.UUID(definition["id"])) != definition["id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        _fail("id must be a canonical lowercase UUID string")
    if not isinstance(definition["content_hash"], str) or not HASH_RE.fullmatch(definition["content_hash"]):
        _fail("content_hash must be 64 lowercase hex chars")
    version = definition["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        _fail("version must be an integer >= 1")
    if not isinstance(definition["source"], str) or not SOURCE_RE.fullmatch(definition["source"]):
        _fail('source must be "learned" or "imported:<sha256>"')
    created = definition["created_at"]
    if not isinstance(created, (int, float)) or isinstance(created, bool) or not math.isfinite(created) or created < 0:
        _fail("created_at must be a unix timestamp")
    if definition["requires"] != requires(definition):
        _fail("requires does not match the tools the steps use")
    if definition["content_hash"] != content_hash(definition):
        _fail("content_hash does not match the skill's content")


def validate(definition, **kwargs) -> None:
    validate_content(definition, **kwargs)
    validate_metadata(definition)
