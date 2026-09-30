"""Persisted settings for the always-listening voice session.

Kept separate from voice_session.py so control.py can read and validate
settings without importing the audio stack (sounddevice, openWakeWord,
Whisper) -- a settings read from the UI must never be what first loads a
model or opens a device.
"""

import json
import re
from pathlib import Path

SETTINGS_FILE = Path(__file__).resolve().parent.parent.parent / "voice_settings.json"

DEFAULTS = {
    "enabled": True,
    # Pretrained openWakeWord names, or paths to a trained .onnx -- a custom
    # "hey Leutheria" model drops in here without code changes.
    "wake_models": ["hey_jarvis"],
    "wake_threshold": 0.5,
    "silence_timeout_s": 180,
    "endpoint_silence_ms": 800,
    "dismiss_phrases": ["thanks", "thank you", "that's all", "that's it", "we're done", "goodbye"],
    "input_device": None,
    # Spoken replies. "elevenlabs" (the default, by user choice 2026-09-30) is
    # a cloud voice needing ELEVENLABS_API_KEY in the environment; without a
    # key every reply falls back to "piper", which is local and free. The key
    # itself is never a setting, so it never lands in this file. tts_voice is
    # the selected backend's voice id; null means that backend's default.
    "tts_backend": "elevenlabs",
    "tts_voice": None,
}

TTS_BACKENDS = ("piper", "elevenlabs")
# Mirrors tts_backends.VOICE_ID_PATTERN (not imported: that module pulls in
# requests and numpy, and this one stays import-light). The id lands in a
# file path or a URL path, so nothing that could escape either is accepted.
_VOICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _is_number(value) -> bool:
    # bool is an int subclass; True must not validate as a threshold of 1.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate(key: str, value):
    """Return an error string, or None if the value is acceptable."""
    if key == "enabled":
        return None if isinstance(value, bool) else "enabled must be true or false"
    if key in ("wake_models", "dismiss_phrases"):
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            return f"{key} must be a list of non-empty strings"
        if key == "wake_models" and not value:
            return "wake_models needs at least one model"
        return None
    if key == "wake_threshold":
        return None if _is_number(value) and 0 < value < 1 else "wake_threshold must be between 0 and 1"
    if key == "silence_timeout_s":
        # null = never: the session ends only when dismissed or stopped by hand.
        ok = value is None or (_is_number(value) and value >= 5)
        return None if ok else "silence_timeout_s must be at least 5, or null for never"
    if key == "endpoint_silence_ms":
        ok = _is_number(value) and 240 <= value <= 5000
        return None if ok else "endpoint_silence_ms must be between 240 and 5000"
    if key == "input_device":
        ok = value is None or (isinstance(value, (int, str)) and not isinstance(value, bool))
        return None if ok else "input_device must be null, a device index, or a device name"
    if key == "tts_backend":
        return None if value in TTS_BACKENDS else f"tts_backend must be one of: {', '.join(TTS_BACKENDS)}"
    if key == "tts_voice":
        ok = value is None or (isinstance(value, str) and _VOICE_ID.match(value))
        return None if ok else "tts_voice must be null or a voice id (letters, digits, - and _)"
    return f"unknown voice setting: {key}"


def load() -> dict:
    """Defaults overlaid with whatever valid keys the file holds. A corrupt
    file or a bad value falls back to the default for that key rather than
    breaking startup -- same stance as preferences.py."""
    settings = dict(DEFAULTS)
    if not SETTINGS_FILE.exists():
        return settings
    try:
        stored = json.loads(SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return settings
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in DEFAULTS and _validate(key, value) is None:
                settings[key] = value
    return settings


def update(partial: dict) -> tuple:
    """Merge a partial settings object into the stored settings and persist.
    All-or-nothing: one invalid key rejects the whole update, so the UI never
    ends up with half of what it asked for applied. Returns (settings, error)."""
    if not isinstance(partial, dict):
        return None, "settings must be an object"
    for key, value in partial.items():
        error = _validate(key, value)
        if error:
            return None, error
    settings = {**load(), **partial}
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    return settings, None
