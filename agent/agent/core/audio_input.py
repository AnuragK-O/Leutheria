import asyncio
import base64
import os
import tempfile

from agent.core import stt


async def transcribe_payload(payload: dict) -> str:
    """Decode a base64 audio payload {"data": ..., "format": "webm"}, run it
    through Whisper, and return the transcript. Whisper's inference is
    blocking/CPU-bound, so it runs in a thread via run_in_executor -- same
    pattern as the LLM calls in agent_loop.
    """
    audio_bytes = base64.b64decode(payload["data"])
    suffix = "." + payload.get("format", "webm")

    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio_bytes)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, stt.transcribe, path)
    finally:
        os.remove(path)
