"""Microphone capture (PortAudio via sounddevice) and WAV helpers."""

from __future__ import annotations

import logging
import queue
import wave
from pathlib import Path

import numpy as np

from hey_claude.speech import FRAME, SAMPLE_RATE

log = logging.getLogger(__name__)


def _device(spec: str):
    if not spec:
        return None
    return int(spec) if spec.isdigit() else spec


class Microphone:
    """16 kHz mono float32 frames of FRAME samples, delivered through a queue."""

    def __init__(self, device: str = "") -> None:
        self.device = _device(device)
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=int(60 * SAMPLE_RATE / FRAME))
        self._stream = None

    def _callback(self, indata, _frames, _time, status) -> None:
        if status:
            log.debug("audio status: %s", status)
        try:
            self.frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            # The consumer stalled for a minute; dropping audio beats growing without bound.
            pass

    def start(self) -> None:
        import sounddevice as sd

        # Ask PortAudio for 16 kHz directly; PipeWire/Pulse resample for us.
        self._stream = sd.InputStream(samplerate=SAMPLE_RATE, blocksize=FRAME, channels=1,
                                      dtype="float32", device=self.device, callback=self._callback)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def read(self, timeout: float = 0.1) -> np.ndarray | None:
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def record(self, seconds: float) -> np.ndarray:
        out, need = [], int(seconds * SAMPLE_RATE)
        while need > 0:
            frame = self.read(timeout=2.0)
            if frame is None:
                raise RuntimeError("no audio from the microphone")
            out.append(frame)
            need -= len(frame)
        return np.concatenate(out)

    def drain(self) -> None:
        while not self.frames.empty():
            self.frames.get_nowait()


def list_devices() -> str:
    import sounddevice as sd

    return str(sd.query_devices())


def read_wav(path: Path) -> np.ndarray:
    """Read a WAV file as 16 kHz mono float32 (resampling linearly if needed)."""
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
    else:
        raise ValueError(f"unsupported WAV sample width {width}")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        n = int(len(data) * SAMPLE_RATE / rate)
        data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data)
    return data.astype(np.float32)


def write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


def frames_of(samples: np.ndarray):
    for i in range(0, len(samples) - FRAME + 1, FRAME):
        yield samples[i:i + FRAME]
