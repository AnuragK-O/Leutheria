from agent.core import inventory, preferences, trust
from agent.core.logging_util import log_event
from agent.skills.registry import delete_skill

# Control messages are the UI's management channel: read the capability
# catalog, change what Leutheria is allowed to do, forget a standing approval.
# They're deliberately handled inline in the server's read loop rather than
# through process_command(), because none of them touch the LLM, none can
# block, and none should be logged or spoken as part of a conversation.
CONTROL_TYPES = {"inventory", "set_preference", "delete_skill", "revoke_trust"}


def handle(payload: dict) -> dict:
    message_type = payload.get("type")

    if message_type == "inventory":
        return {"ok": True, **inventory.snapshot()}

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
