from agent.core import inventory, preferences, trust
from agent.core.logging_util import log_event
from agent.core.permissions import get_all_active_permissions
from agent.core.skill_learning_v2 import export_skill_package, import_skill_package
from agent.core.trace import default_trace_manager
from agent.skills.registry_v2 import SKILLS, delete_skill, get_all_manifests, register_skill

CONTROL_TYPES = {
    "inventory",
    "set_preference",
    "delete_skill",
    "revoke_trust",
    "get_metrics",
    "get_skills",
    "export_skill",
    "import_skill",
    "get_trace",
    "get_permissions",
}


def handle(payload: dict) -> dict:
    message_type = payload.get("type")

    if message_type == "inventory":
        return {"ok": True, **inventory.snapshot()}

    if message_type == "get_metrics":
        return {"ok": True, "metrics": default_trace_manager.get_metrics()}

    if message_type == "get_skills":
        manifests = [m.to_dict() for m in get_all_manifests()]
        return {"ok": True, "skills": manifests}

    if message_type == "export_skill":
        name = payload.get("name")
        skill = SKILLS.get(name)
        if not skill or "manifest" not in skill:
            return {"ok": False, "error": f"no such skill: {name}"}
        m = skill["manifest"]
        package = {
            "format_version": "1.0",
            "manifest": m.to_dict(),
        }
        return {"ok": True, "name": name, "package": package}

    if message_type == "import_skill":
        package_str = payload.get("package")
        if not package_str:
            return {"ok": False, "error": "missing package content"}
        try:
            skill = import_skill_package(package_str)
            register_skill(skill)
            log_event("skill_imported", name=skill.name)
            return {"ok": True, "name": skill.name}
        except Exception as e:
            return {"ok": False, "error": f"import failed: {str(e)}"}

    if message_type == "get_trace":
        task_id = payload.get("task_id")
        if not task_id:
            return {"ok": False, "error": "missing task_id"}
        task = default_trace_manager.get_task(task_id)
        steps = [s.__dict__ for s in default_trace_manager.get_task_steps(task_id)]
        corrections = [c.__dict__ for c in default_trace_manager.get_task_corrections(task_id)]
        return {
            "ok": True,
            "task": task.__dict__ if task else None,
            "steps": steps,
            "corrections": corrections,
        }

    if message_type == "get_permissions":
        return {"ok": True, "permissions": get_all_active_permissions()}

    if message_type == "set_preference":
        name = payload.get("name")
        if not name:
            return {"ok": False, "error": "missing name"}
        if payload.get("reset"):
            preferences.clear(name)
            log_event("preference_reset", name=name)
            return {"ok": True, "name": name}
        entry = preferences.set_preference(
            name,
            enabled=payload.get("enabled"),
            requires_confirmation=payload.get("requires_confirmation"),
        )
        log_event("preference_changed", name=name, **entry)
        return {"ok": True, "name": name, **entry}

    if message_type == "delete_skill":
        name = payload.get("name")
        if not name:
            return {"ok": False, "error": "missing name"}
        result = delete_skill(name)
        if result.get("ok"):
            log_event("skill_deleted", name=name)
        return result

    if message_type == "revoke_trust":
        key = payload.get("key")
        if not key:
            return {"ok": False, "error": "missing key"}
        result = trust.revoke(key)
        if result.get("ok"):
            log_event("trust_revoked", key=key)
        return result

    return {"ok": False, "error": f"unknown control message: {message_type}"}
