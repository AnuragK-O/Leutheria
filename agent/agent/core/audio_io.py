"""Audio in and out for the sidecar: mic frames for the voice session, and
speaker playback for TTS.

Every frame source here is an async iterator of 80 ms int16 mono frames at
16 kHz (1280 samples) -- openWakeWord's native chunk size. The voice session
only ever sees that iterator, which is what lets a WAV file stand in for the
microphone and drive the whole state machine in a test (scripts/voice_sim.py).
"""

import asyncio
import io
import threading
import time
import wave

import numpy as np

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms
FRAME_MS = 80

# Held by speaker playback and by a device-list refresh: PortAudio can't be
# re-initialised under a stream that's still playing. (Mic capture is kept
# out of the way by the voice session closing its stream first.)
_portaudio_lock = threading.Lock()

# ~2.5 s of backlog. If the consumer falls further behind than this (a long
# GC pause, a slow model load), the oldest audio is dropped: stale audio is
# worse than a gap for a live listener.
_MAX_QUEUED_FRAMES = 32


def list_input_devices() -> list:
    """Enumerate input devices without opening any of them -- opening a stream
    is what triggers the macOS microphone prompt, listing does not.

    PortAudio snapshots the device list when it initializes, so a device
    plugged in later doesn't appear here until reinitialize_devices() runs --
    which the voice session does on request, between closing its mic stream
    and reopening it (NOTES #6)."""
    import sounddevice as sd

    try:
        default_index = sd.query_devices(kind="input")["index"]
    except Exception:  # no default input device at all
        default_index = None
    return [
        {
            "index": i,
            "name": d["name"],
            "channels": d["max_input_channels"],
            "default": i == default_index,
        }
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]


async def mic_frames(device=None):
    """Live microphone frames. PortAudio calls back on its own thread, so each
    block is handed to the event loop with call_soon_threadsafe; the stream
    is closed when the consumer stops iterating (aclose/break)."""
    import sounddevice as sd

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _enqueue(frame):
        if queue.qsize() >= _MAX_QUEUED_FRAMES:
            queue.get_nowait()
        queue.put_nowait(frame)

    def _callback(indata, frames, time_info, status):
        loop.call_soon_threadsafe(_enqueue, indata[:, 0].copy())

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_SAMPLES,
        device=device,
        callback=_callback,
    )
    stream.start()
    try:
        while True:
            yield await queue.get()
    finally:
        stream.stop()
        stream.close()


def load_wav_16k(path: str) -> np.ndarray:
    """Read a WAV file as 16 kHz mono int16, resampling if needed."""
    with wave.open(path, "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM WAV is supported")
    audio = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return resample(audio, rate, SAMPLE_RATE)


def resample(audio: np.ndarray, rate_in: int, rate_out: int) -> np.ndarray:
    if rate_in == rate_out:
        return audio
    from scipy.signal import resample_poly

    divisor = np.gcd(rate_in, rate_out)
    out = resample_poly(audio.astype(np.float32), rate_out // divisor, rate_in // divisor)
    return np.clip(out, -32768, 32767).astype(np.int16)


async def array_frames(audio: np.ndarray, realtime: float = 1.0, pad_silence: bool = False):
    """Frames from an in-memory clip, paced like a real mic (realtime=1.0 is
    wall-clock speed). Pacing matters: playback muting and its 300 ms tail
    run on wall-clock time, so an unpaced source would race past them.
    pad_silence keeps yielding silent frames after the clip ends, the way a
    real mic never runs out -- needed to let a silence timeout play out."""
    frames = [audio[i : i + FRAME_SAMPLES] for i in range(0, len(audio), FRAME_SAMPLES)]
    if frames and len(frames[-1]) < FRAME_SAMPLES:
        frames[-1] = np.pad(frames[-1], (0, FRAME_SAMPLES - len(frames[-1])))
    silence = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    interval = FRAME_MS / 1000 / realtime if realtime else 0
    start = time.monotonic()
    count = 0
    while True:
        if count < len(frames):
            frame = frames[count]
        elif pad_silence:
            frame = silence
        else:
            return
        count += 1
        if interval:
            delay = start + count * interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        else:
            await asyncio.sleep(0)
        yield frame


async def wav_frames(path: str, realtime: float = 1.0, pad_silence: bool = False):
    async for frame in array_frames(load_wav_16k(path), realtime, pad_silence):
        yield frame


def wav_bytes_to_array(data: bytes) -> tuple:
    """(int16 samples, sample rate) from in-memory WAV bytes."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        rate = wav.getframerate()
        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    return audio, rate


def play_blocking(audio: np.ndarray, rate: int) -> None:
    """Play to the default output device and return when playback ends. Run
    this in an executor -- it blocks for the length of the clip."""
    import sounddevice as sd

    with _portaudio_lock:
        sd.play(audio, rate, blocking=True)


def reinitialize_devices() -> None:
    """Make PortAudio re-read the device list, so a mic plugged in since the
    agent started can be listed and opened. Blocking; run in an executor.

    Only safe with no stream open: terminating PortAudio under a live stream
    crashes the process. Playback is excluded by _portaudio_lock; the caller
    (VoiceSession.refresh_devices) guarantees the mic stream is closed.
    _terminate/_initialize are private but are sounddevice's documented way
    to do this -- there's no public equivalent."""
    import sounddevice as sd

    with _portaudio_lock:
        sd._terminate()
        sd._initialize()
