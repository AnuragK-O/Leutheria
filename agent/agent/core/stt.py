import threading

import whisper

MODEL_NAME = "base"  # good speed/accuracy tradeoff for short push-to-talk clips on CPU

_model = None
# Push-to-talk and the live voice session each transcribe from their own
# executor thread, and one Whisper model instance isn't safe to run from two
# threads at once -- nor would running both in parallel be any faster on CPU.
_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        _model = whisper.load_model(MODEL_NAME)
    return _model


def transcribe(path: str) -> str:
    with _lock:
        result = _get_model().transcribe(path)
    return result["text"].strip()


def transcribe_segments(audio) -> list:
    """Transcribe a 16 kHz mono float32 array and return Whisper's segments,
    each with its `text` and `no_speech_prob`. The voice session needs the
    per-segment no-speech probability to throw away Whisper's well-known
    hallucinations on near-silence ("Thank you.", "Thanks for watching.") --
    which would otherwise read as a dismissal and end the session."""
    with _lock:
        result = _get_model().transcribe(audio, fp16=False)
    return result.get("segments", [])
