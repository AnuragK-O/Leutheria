from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from agent.core.action import RiskLevel, TOOL_ACTION_PROFILES
from agent.core.trace import TraceStepRecord, default_trace_manager
from agent.skills.schema import SkillManifest

NOT_APPLICABLE = object()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_trace_signature(steps: List[Dict[str, Any]]) -> List[str]:
    return [s.get("tool") or s.get("action_type", "") for s in steps]


def infer_permissions_and_risk(steps: List[Dict[str, Any]]) -> Tuple[List[str], str]:
    permissions = set()
    highest_risk = RiskLevel.LOW

    for step in steps:
        tool_name = step.get("tool", "")
        profile = TOOL_ACTION_PROFILES.get(tool_name)
        if profile:
            permissions.update(profile["required_permissions"])
            risk = profile["risk_level"]
            if risk == RiskLevel.HIGH or highest_risk == RiskLevel.HIGH:
                highest_risk = RiskLevel.HIGH
            elif risk == RiskLevel.MEDIUM and highest_risk == RiskLevel.LOW:
                highest_risk = RiskLevel.MEDIUM

    return sorted(list(permissions)), highest_risk.value


def infer_command_requirements(steps: List[Dict[str, Any]]) -> List[str]:
    commands = set()
    for step in steps:
        if step.get("tool") == "run_command":
            cmd = step.get("args", {}).get("cmd", "").strip()
            if cmd:
                # First token before space
                token = cmd.split()[0]
                # Filter out shell builtins or common symbols
                if token not in ("echo", "cd", "pwd", "export"):
                    commands.add(token)
    return sorted(list(commands))


def build_validation_checks(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    checks = []
    for step in steps:
        tool = step.get("tool")
        args = step.get("args", {})
        if tool in ("create_folder", "create_file"):
            checks.append({
                "check": "path_exists",
                "tool": "list_files",
                "args": {"path": args.get("path", "")},
                "expected": "success",
            })
    return checks


def propose_skill_from_trace(
    backend,
    trace_steps: List[Dict[str, Any]],
    user_request: str = "",
    past_trace_steps: Optional[List[Dict[str, Any]]] = None,
    task_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Propose a SkillManifest from execution trace.
    
    If past_trace_steps is provided, diffs the two runs for confident parameterization.
    Otherwise, generates from single run and flags uncertain parameters for user confirmation.
    """
    if len(trace_steps) < 2:
        return None

    permissions, risk_level = infer_permissions_and_risk(trace_steps)
    req_commands = infer_command_requirements(trace_steps)
    validation = build_validation_checks(trace_steps)

    summarized_steps = [{"tool": s["tool"], "args": s["args"]} for s in trace_steps]

    if past_trace_steps:
        summarized_past = [{"tool": s["tool"], "args": s["args"]} for s in past_trace_steps]
        prompt = f"""You are analyzing two successful runs of the same multi-step desktop workflow.
Generalize them into a declarative skill definition.

Example 1:
{json.dumps(summarized_past, indent=2)}

Example 2:
{json.dumps(summarized_steps, indent=2)}

User Request: {user_request}

Respond with ONLY a JSON object (no markdown fences, no prose):
{{
  "name": "snake_case_skill_name",
  "description": "Clear sentence describing what this workflow accomplishes.",
  "parameters": {{
    "<param_name>": {{
      "type": "string",
      "description": "description of parameter",
      "required": true
    }}
  }},
  "steps": [
    {{
      "tool": "<tool_name>",
      "args": {{"<arg_key>": "<arg_value_with_{{param_name}}_placeholders>"}},
      "description": "<short description of this step>"
    }}
  ]
}}
"""
    else:
        prompt = f"""A user just completed a multi-step desktop task. Propose a reusable declarative skill.

Original Request: {user_request or "(not specified)"}

Actions Executed:
{json.dumps(summarized_steps, indent=2)}

Respond with ONLY a JSON object (no markdown fences, no prose):
{{
  "name": "snake_case_skill_name",
  "description": "Clear sentence describing what this workflow accomplishes.",
  "parameters": {{
    "<param_name>": {{
      "type": "string",
      "description": "description of parameter",
      "required": true
    }}
  }},
  "steps": [
    {{
      "tool": "<tool_name>",
      "args": {{"<arg_key>": "<arg_value_with_{{param_name}}_placeholders>"}},
      "description": "<short description of this step>"
    }}
  ],
  "uncertain_params": [
    {{
      "name": "<param_name>",
      "guessed_value": "<the concrete value seen in this run>",
      "question": "Does this value vary each time, or should it always be this fixed value?"
    }}
  ]
}}
Any parameter that was explicitly in the user's request (like a specific project name) is a confident parameter.
Locations or options that only appeared once should be flagged under uncertain_params.
"""

    turn = backend.generate([{"role": "user", "content": prompt}], [])
    text = turn.text.strip()
    
    # Extract JSON
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None

    try:
        raw_def = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None

    # Merge inferred metadata
    manifest_data = {
        "name": raw_def.get("name", "learned_workflow"),
        "description": raw_def.get("description", "Learned desktop workflow"),
        "parameters": raw_def.get("parameters", {}),
        "requirements": {"applications": [], "commands": req_commands},
        "permissions": permissions,
        "risk_level": risk_level,
        "steps": raw_def.get("steps", summarized_steps),
        "validation": validation,
        "provenance": {
            "creator": "user_learned",
            "created_at": _now_iso(),
            "source_task_id": task_id,
            "source_signature": extract_trace_signature(trace_steps),
        },
        "autonomy_tier": "copilot",
    }

    manifest = SkillManifest.from_dict(manifest_data)
    proposal_dict = manifest.to_dict()
    proposal_dict["uncertain_params"] = raw_def.get("uncertain_params", [])
    return proposal_dict


def finalize_learned_skill(proposal: Dict[str, Any], resolutions: Dict[str, bool]) -> SkillManifest:
    """Apply user answers to uncertain_params and produce a clean SkillManifest.
    
    resolutions maps param_name -> bool (True = varies as param, False = bake fixed value).
    """
    data = json.loads(json.dumps(proposal))
    uncertain = data.pop("uncertain_params", [])

    def _bake(val, name, literal):
        if isinstance(val, str):
            return val.replace("{" + name + "}", literal)
        if isinstance(val, dict):
            return {k: _bake(v, name, literal) for k, v in val.items()}
        return val

    for item in uncertain:
        name = item["name"]
        # If user chose False (fixed value), bake into steps
        if resolutions.get(name, True) is False:
            literal = item.get("guessed_value", "")
            for step in data.get("steps", []):
                step["args"] = _bake(step["args"], name, literal)
            data["parameters"].pop(name, None)

    return SkillManifest.from_dict(data)


def export_skill_package(skill: SkillManifest, output_path: Path) -> Path:
    """Export skill as a portable .leutheria-skill package (JSON formatted with package manifest)."""
    package = {
        "format_version": "1.0",
        "exported_at": _now_iso(),
        "manifest": skill.to_dict(),
        "checksum": str(hash(skill.to_json())),
    }
    output_path.write_text(json.dumps(package, indent=2))
    return output_path


def import_skill_package(package_data: str) -> SkillManifest:
    """Import and validate a portable skill package.
    
    Enforces schema validation and resets permissions so imported skills never
    gain unconfirmed execution rights automatically.
    """
    try:
        raw = json.loads(package_data)
    except Exception as e:
        raise ValueError(f"Invalid package data: {e}")

    manifest_data = raw.get("manifest") if isinstance(raw, dict) and "manifest" in raw else raw
    skill = SkillManifest.from_dict(manifest_data)
    errors = skill.validate()
    if errors:
        raise ValueError(f"Invalid skill package: {'; '.join(errors)}")

    # Mark origin as imported and reset autonomy statistics for safety
    skill.provenance["imported_at"] = _now_iso()
    skill.provenance["origin"] = "imported"
    skill.autonomy_tier = "guide"  # Always starts at Guide for safety
    skill.run_count = 0
    skill.success_count = 0
    skill.intervention_count = 0

    return skill
