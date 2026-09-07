import json
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from agent.core import preferences, trust
from agent.core.action import ExecutionMode, RiskLevel, StructuredAction

PERMISSIONS_FILE = Path(__file__).resolve().parent.parent.parent / "permissions.json"


class PermissionScope(str, Enum):
    ONCE = "once"
    TASK = "task"
    SKILL = "skill"
    SESSION = "session"
    PERMANENT = "permanent"


# In-memory granted permissions
# Maps task_id -> set of granted permission strings e.g. {"terminal:execute", "filesystem:create"}
_task_grants: Dict[str, Set[str]] = {}

# Maps skill_name -> set of granted permission strings
_skill_grants: Dict[str, Set[str]] = {}

# Set of session-level granted permission strings
_session_grants: Set[str] = set()

# Permanent grants loaded from disk
_permanent_grants: Set[str] = set()


def _load_permanent():
    global _permanent_grants
    if PERMISSIONS_FILE.exists():
        try:
            data = json.loads(PERMISSIONS_FILE.read_text())
            if isinstance(data, list):
                _permanent_grants = set(data)
        except Exception:
            _permanent_grants = set()


def _save_permanent():
    PERMISSIONS_FILE.write_text(json.dumps(sorted(list(_permanent_grants)), indent=2))


_load_permanent()


def grant_permission(
    permission: str,
    scope: PermissionScope = PermissionScope.SESSION,
    context_id: Optional[str] = None,
) -> None:
    """Grant a permission under a specific scope (TASK, SKILL, SESSION, PERMANENT)."""
    if scope == PermissionScope.TASK and context_id:
        _task_grants.setdefault(context_id, set()).add(permission)
    elif scope == PermissionScope.SKILL and context_id:
        _skill_grants.setdefault(context_id, set()).add(permission)
    elif scope == PermissionScope.SESSION:
        _session_grants.add(permission)
    elif scope == PermissionScope.PERMANENT:
        _permanent_grants.add(permission)
        _save_permanent()


def revoke_permission(permission: str, scope: PermissionScope, context_id: Optional[str] = None) -> None:
    if scope == PermissionScope.TASK and context_id and context_id in _task_grants:
        _task_grants[context_id].discard(permission)
    elif scope == PermissionScope.SKILL and context_id and context_id in _skill_grants:
        _skill_grants[context_id].discard(permission)
    elif scope == PermissionScope.SESSION:
        _session_grants.discard(permission)
    elif scope == PermissionScope.PERMANENT:
        _permanent_grants.discard(permission)
        _save_permanent()


def is_permission_granted(
    permission: str,
    task_id: Optional[str] = None,
    skill_name: Optional[str] = None,
) -> bool:
    """Check if a required permission is satisfied by any active grant."""
    if permission in _permanent_grants:
        return True
    if permission in _session_grants:
        return True
    if task_id and task_id in _task_grants and permission in _task_grants[task_id]:
        return True
    if skill_name and skill_name in _skill_grants and permission in _skill_grants[skill_name]:
        return True
    return False


def evaluate_action_authorization(
    tool_name: str,
    action: StructuredAction,
    mode: ExecutionMode = ExecutionMode.COPILOT,
    task_id: Optional[str] = None,
    skill_name: Optional[str] = None,
) -> Tuple[bool, str]:
    """Evaluate whether an action is authorized to execute immediately without user approval.
    
    Returns (authorized, reason).
    If authorized is False, execution must pause for human confirmation / intervention.
    """
    # 1. First check if explicitly disabled in preferences
    if not preferences.is_enabled(tool_name):
        return False, f"{tool_name} is currently disabled in preferences"

    # 2. Check if the exact (tool, args) pair is trusted in trust.py
    if trust.is_trusted(tool_name, action.parameters):
        return True, "Exact command is pre-trusted"

    # 3. Check Library preference overrides: if user explicitly set "always ask before using this"
    override = preferences.overrides_for(tool_name).get("requires_confirmation")
    if override is True:
        return False, f"User preference requires confirmation for {tool_name}"

    # 4. Check Guide Mode: In Guide mode, agent NEVER performs real mutating actions
    if mode == ExecutionMode.GUIDE:
        if action.action_type in ("ghost_guidance", "system_inspect", "read_file", "list_files"):
            return True, "Guidance and read-only inspection allowed in Guide mode"
        return False, f"Guide mode: action '{action.description}' requires user to perform it"

    # 5. Check if all required permissions are granted
    missing_permissions = [
        p for p in action.required_permissions
        if not is_permission_granted(p, task_id=task_id, skill_name=skill_name)
    ]

    # 6. Autopilot mode:
    if mode == ExecutionMode.AUTOPILOT:
        # High-risk actions STILL require authorization unless explicitly granted
        if action.risk_level == RiskLevel.HIGH and missing_permissions:
            return False, f"High-risk action requires permission: {', '.join(missing_permissions)}"
        return True, "Autopilot authorized"

    # 7. Copilot mode (Default):
    # LOW risk actions with permissions (or no permissions required) execute automatically
    if action.risk_level == RiskLevel.LOW and not missing_permissions:
        return True, "Low-risk action automatically approved in Copilot mode"

    # MEDIUM risk actions execute only if all required permissions are already granted
    if action.risk_level == RiskLevel.MEDIUM and not missing_permissions:
        return True, "Medium-risk action pre-authorized by granted permissions"

    # Otherwise (HIGH risk or missing permissions), confirmation is required
    return False, f"Action requires user confirmation (Risk: {action.risk_level.value})"


def get_all_active_permissions() -> Dict[str, Any]:
    """Snapshot for UI Permissions view."""
    return {
        "permanent": sorted(list(_permanent_grants)),
        "session": sorted(list(_session_grants)),
        "skills": {k: sorted(list(v)) for k, v in _skill_grants.items()},
    }
