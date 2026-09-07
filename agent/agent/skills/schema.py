from dataclasses import asdict, dataclass, field
import json
import re
from typing import Any, Dict, List, Optional
import uuid


@dataclass
class SkillParameter:
    name: str
    type: str = "string"  # string, path, integer, boolean
    description: str = ""
    default: Optional[Any] = None
    required: bool = True


@dataclass
class SkillStep:
    tool: str
    args: Dict[str, Any]
    description: str = ""
    risk_level: str = "low"
    required_permissions: List[str] = field(default_factory=list)


@dataclass
class SkillManifest:
    name: str
    description: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    version: str = "1.0.0"
    parameters: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    requirements: Dict[str, List[str]] = field(default_factory=lambda: {"applications": [], "commands": []})
    permissions: List[str] = field(default_factory=list)
    risk_level: str = "medium"
    steps: List[Dict[str, Any]] = field(default_factory=list)
    validation: List[Dict[str, Any]] = field(default_factory=list)
    rollback: List[Dict[str, Any]] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)
    autonomy_tier: str = "guide"  # guide, copilot, autopilot
    run_count: int = 0
    success_count: int = 0
    intervention_count: int = 0
    is_enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def to_input_schema(self) -> Dict[str, Any]:
        """Convert manifest parameters into a JSON Schema for LLM tool calling."""
        props = {}
        required = []
        for p_name, p_info in self.parameters.items():
            props[p_name] = {
                "type": p_info.get("type", "string"),
                "description": p_info.get("description", ""),
            }
            if p_info.get("required", True):
                required.append(p_name)
        return {
            "type": "object",
            "properties": props,
            "required": required,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillManifest":
        data = dict(data)
        # Normalize name: snake_case or kebab-case
        name = data.get("name", "unnamed_skill")
        name = re.sub(r"[^a-zA-Z0-9_\-]", "_", name).lower()
        data["name"] = name

        # Backward compatibility with v1 skill definitions
        if "input_schema" in data and not data.get("parameters"):
            props = data["input_schema"].get("properties", {})
            req = set(data["input_schema"].get("required", []))
            params = {}
            for k, v in props.items():
                params[k] = {
                    "type": v.get("type", "string"),
                    "description": v.get("description", ""),
                    "required": k in req,
                }
            data["parameters"] = params

        valid_fields = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    @classmethod
    def from_json(cls, json_str: str) -> "SkillManifest":
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON format for skill manifest: {e}")
        return cls.from_dict(data)

    def validate(self) -> List[str]:
        """Check manifest validity. Returns list of errors (empty if valid)."""
        errors = []
        if not self.name or not re.match(r"^[a-zA-Z0-9_\-]+$", self.name):
            errors.append(f"Invalid skill name: '{self.name}'. Must be alphanumeric with '-' or '_'.")
        if not self.description:
            errors.append("Skill description is required.")
        if not self.steps:
            errors.append("Skill must contain at least one step.")
        for idx, step in enumerate(self.steps):
            if "tool" not in step:
                errors.append(f"Step {idx} missing 'tool' field.")
            if "args" not in step or not isinstance(step["args"], dict):
                errors.append(f"Step {idx} missing or invalid 'args' dictionary.")
        return errors
