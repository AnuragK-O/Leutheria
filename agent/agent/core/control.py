from agent.core import audio_io, inventory, preferences, session, trust, tts_backends, voice_session, voice_settings
from agent.core.logging_util import log_event
from agent.skills.registry import delete_skill

# Control messages are the UI's management channel: read the capability
# catalog, change what Leutheria is allowed to do, forget a standing approval.
# They're deliberately handled inline in the server's read loop rather than
# through process_command(), because none of them touch the LLM, none can
# block, and none should be logged or spoken as part of a conversation.
CONTROL_TYPES = {
    "inventory",
    "set_preference",
    "delete_skill",
    "revoke_trust",
    "revoke_grant",
    "voice_session",
    "get_voice_settings",
    "set_voice_settings",
    "list_input_devices",
    "list_wake_models",
}


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

    if message_type == "revoke_grant":
        app = payload.get("app")
        if not app:
            return {"ok": False, "error": "missing app"}
        if not session.revoke(app):
            return {"ok": False, "error": f"no live grant for {app}"}
        log_event("grant_revoked", app=app)
        return {"ok": True, "app": app}

    if message_type == "voice_session":
        live = voice_session.current()
        if live is None:
            return {"ok": False, "error": "voice is unavailable (see voice_error in the log)"}
        result = live.request(payload.get("action"))
        if result.get("ok"):
            log_event("voice_session_requested", action=payload.get("action"))
        return result

    if message_type == "get_voice_settings":
        # tts_available is booleans only (is a key set?), never the key itself.
        return {"ok": True, "settings": voice_settings.load(), "tts_available": tts_backends.availability()}

    if message_type == "set_voice_settings":
        settings, error = voice_settings.update(payload.get("settings"))
        if error:
            return {"ok": False, "error": error}
        # Applied live: enabling opens the mic, disabling closes it (ending
        # any session), a new wake model or input device restarts capture.
        live = voice_session.current()
        if live is not None:
            live.apply_settings(settings)
        log_event("voice_settings_changed", settings=payload.get("settings"))
        return {"ok": True, "settings": settings}

    if message_type == "list_input_devices":
        # query_devices only -- never opens a stream, so it can't be what
        # triggers the macOS mic prompt or disturbs the live session's capture.
        try:
            return {"ok": True, "devices": audio_io.list_input_devices()}
        except Exception as e:  # PortAudio missing or broken
            return {"ok": False, "error": f"couldn't list input devices: {e}"}

    if message_type == "list_wake_models":
        # Names only: no model is loaded or downloaded. `paths` (optional) are
        # custom .onnx entries the UI wants checked for existence.
        paths = payload.get("paths") or []
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            return {"ok": False, "error": "paths must be a list of strings"}
        try:
            pretrained = voice_session.available_wake_models()
        except Exception as e:
            return {"ok": False, "error": f"couldn't list wake models: {e}"}
        return {
            "ok": True,
            "pretrained": pretrained,
            "paths": [{"path": p, "exists": voice_session.wake_model_path_exists(p)} for p in paths],
        }

    return {"ok": False, "error": f"unknown control message: {message_type}"}
