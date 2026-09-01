import whisper

MODEL_NAME = "base"  # good speed/accuracy tradeoff for short push-to-talk clips on CPU

_model = None


def _get_model():
    global _model
    if _model is None:
        _model = whisper.load_model(MODEL_NAME)
    return _model


def transcribe(path: str) -> str:
    result = _get_model().transcribe(path)
    return result["text"].strip()
