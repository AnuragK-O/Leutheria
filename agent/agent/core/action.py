from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExecutionMode(str, Enum):
    GUIDE = "guide"
    COPILOT = "copilot"
    AUTOPILOT = "autopilot"


@dataclass
class StructuredAction:
    action_type: str
    parameters: Dict[str, Any]
    description: str
    reason: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    required_permissions: List[str] = field(default_factory=list)
    requires_confirmation: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action_type,
            "parameters": self.parameters,
            "description": self.description,
            "reason": self.reason,
            "risk": self.risk_level.value,
            "required_permissions": self.required_permissions,
            "requires_confirmation": self.requires_confirmation,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StructuredAction":
        risk = RiskLevel(data.get("risk", "low"))
        return cls(
            action_type=data.get("action", "unknown"),
            parameters=data.get("parameters", {}),
            description=data.get("description", ""),
            reason=data.get("reason", ""),
            risk_level=risk,
            required_permissions=data.get("required_permissions", []),
            requires_confirmation=data.get("requires_confirmation", False),
            metadata=data.get("metadata", {}),
        )


# Mapping of registered tool names to their structured action semantics
TOOL_ACTION_PROFILES = {
    "open_app": {
        "action_type": "open_application",
        "risk_level": RiskLevel.LOW,
        "required_permissions": ["app:launch"],
        "desc_template": "Open application '{name}'",
    },
    "open_url": {
        "action_type": "open_url",
        "risk_level": RiskLevel.LOW,
        "required_permissions": ["network:browse"],
        "desc_template": "Open URL '{url}'",
    },
    "get_clipboard": {
        "action_type": "clipboard_read",
        "risk_level": RiskLevel.LOW,
        "required_permissions": ["clipboard:read"],
        "desc_template": "Read clipboard contents",
    },
    "set_clipboard": {
        "action_type": "clipboard_write",
        "risk_level": RiskLevel.LOW,
        "required_permissions": ["clipboard:write"],
        "desc_template": "Write to clipboard",
    },
    "read_file": {
        "action_type": "read_file",
        "risk_level": RiskLevel.LOW,
        "required_permissions": ["filesystem:read"],
        "desc_template": "Read file at '{path}'",
    },
    "list_files": {
        "action_type": "list_files",
        "risk_level": RiskLevel.LOW,
        "required_permissions": ["filesystem:read"],
        "desc_template": "List files in '{path}'",
    },
    "show_notification": {
        "action_type": "show_notification",
        "risk_level": RiskLevel.LOW,
        "required_permissions": ["system:notify"],
        "desc_template": "Show notification '{title}'",
    },
    "take_screenshot": {
        "action_type": "capture_screen",
        "risk_level": RiskLevel.LOW,
        "required_permissions": ["screen:capture"],
        "desc_template": "Take screen screenshot",
    },
    "list_running_apps": {
        "action_type": "list_running_apps",
        "risk_level": RiskLevel.LOW,
        "required_permissions": ["system:inspect"],
        "desc_template": "List running applications",
    },
    "get_battery_status": {
        "action_type": "get_battery_status",
        "risk_level": RiskLevel.LOW,
        "required_permissions": ["system:inspect"],
        "desc_template": "Get battery status",
    },
    "get_disk_space": {
        "action_type": "get_disk_space",
        "risk_level": RiskLevel.LOW,
        "required_permissions": ["system:inspect"],
        "desc_template": "Get disk space",
    },
    "get_current_datetime": {
        "action_type": "get_current_datetime",
        "risk_level": RiskLevel.LOW,
        "required_permissions": [],
        "desc_template": "Get current date/time",
    },
    "ghost_guidance": {
        "action_type": "ghost_guidance",
        "risk_level": RiskLevel.LOW,
        "required_permissions": ["ui:guide"],
        "desc_template": "Show guidance highlight for '{label}'",
    },
    # Medium risk operations: creating or modifying filesystem
    "create_folder": {
        "action_type": "create_folder",
        "risk_level": RiskLevel.MEDIUM,
        "required_permissions": ["filesystem:create"],
        "desc_template": "Create folder at '{path}'",
    },
    "append_text": {
        "action_type": "modify_file",
        "risk_level": RiskLevel.MEDIUM,
        "required_permissions": ["filesystem:modify"],
        "desc_template": "Append text to '{path}'",
    },
    "copy_file": {
        "action_type": "copy_file",
        "risk_level": RiskLevel.MEDIUM,
        "required_permissions": ["filesystem:read", "filesystem:create"],
        "desc_template": "Copy from '{source}' to '{destination}'",
    },
    "move_file": {
        "action_type": "move_file",
        "risk_level": RiskLevel.MEDIUM,
        "required_permissions": ["filesystem:modify"],
        "desc_template": "Move from '{source}' to '{destination}'",
    },
    # High risk operations: arbitrary terminal command, file deletion
    "run_command": {
        "action_type": "terminal",
        "risk_level": RiskLevel.HIGH,
        "required_permissions": ["terminal:execute"],
        "desc_template": "Execute shell command: '{cmd}'",
    },
    "trash_file": {
        "action_type": "delete_file",
        "risk_level": RiskLevel.HIGH,
        "required_permissions": ["filesystem:delete"],
        "desc_template": "Move file or folder '{path}' to Trash",
    },
}


def normalize_tool_action(tool_name: str, args: Dict[str, Any], reason: str = "") -> StructuredAction:
    """Translate a tool invocation into a normalized StructuredAction."""
    profile = TOOL_ACTION_PROFILES.get(tool_name)
    if profile is None:
        # Unknown or custom capability defaults to MEDIUM risk
        return StructuredAction(
            action_type=tool_name,
            parameters=args,
            description=f"Invoke tool '{tool_name}'",
            reason=reason,
            risk_level=RiskLevel.MEDIUM,
            required_permissions=[f"custom:{tool_name}"],
            requires_confirmation=True,
        )

    # Format human-readable description
    try:
        desc = profile["desc_template"].format(**{k: str(v) for k, v in args.items()})
    except (KeyError, IndexError, ValueError):
        desc = f"Invoke {tool_name} with {args}"

    risk = profile["risk_level"]
    # High risk actions require confirmation by default
    requires_conf = (risk == RiskLevel.HIGH)

    return StructuredAction(
        action_type=profile["action_type"],
        parameters=args,
        description=desc,
        reason=reason,
        risk_level=risk,
        required_permissions=list(profile["required_permissions"]),
        requires_confirmation=requires_conf,
    )
