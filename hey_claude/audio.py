"""Microphone capture (PortAudio via sounddevice) and WAV helpers."""

from __future__ import annotations

import logging
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from hey_claude.speech import FRAME, SAMPLE_RATE

log = logging.getLogger(__name__)


def _device(spec: str):
    if not spec:
        return None
    return int(spec) if spec.isdigit() else spec


class MicrophoneWedged(RuntimeError):
    """PortAudio did not let go of a dead stream; only a fresh process recovers."""


_portaudio_opened = False


def _open_portaudio(device, callback):
    import sounddevice as sd

    global _portaudio_opened
    if _portaudio_opened:
        # PortAudio enumerates devices once; refresh so a restarted audio server is found.
        sd._terminate()
        sd._initialize()
    _portaudio_opened = True
    # Ask PortAudio for 16 kHz directly; PipeWire/Pulse resample for us.
    return sd.InputStream(samplerate=SAMPLE_RATE, blocksize=FRAME, channels=1,
                          dtype="float32", device=device, callback=callback)


class Microphone:
    """16 kHz mono float32 frames of FRAME samples, delivered through a queue.

    The daemon calls keep_alive() from its loop: it opens the stream, retries with
    backoff while the audio server is unavailable, and replaces a stream that has
    gone silent (what PortAudio does when PipeWire restarts underneath it).
    """

    STALL_SECONDS = 3.0
    RETRY_MAX = 30.0

    def __init__(self, device: str = "", *, open_stream: Callable = _open_portaudio,
                 clock: Callable[[], float] = time.monotonic, close_timeout: float = 2.0) -> None:
        self.device = _device(device)
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=int(60 * SAMPLE_RATE / FRAME))
        self._stream = None
        self._open_stream = open_stream
        self._clock = clock
        self._close_timeout = close_timeout
        self._opened_at = 0.0
        self._last_frame: float | None = None
        self._delay = 0.0
        self._next_try = 0.0

    def _callback(self, indata, _frames, _time, status) -> None:
        self._last_frame = self._clock()
        if status:
            log.debug("audio status: %s", status)
        try:
            self.frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            # The consumer stalled for a minute; dropping audio beats growing without bound.
            pass

    def _open(self) -> None:
        self._last_frame = None
        self._opened_at = self._clock()
        stream = self._open_stream(self.device, self._callback)
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        self._stream = stream

    def _close(self) -> bool:
        """Abort and close the stream; False if PortAudio hung doing so."""
        stream, self._stream = self._stream, None
        if stream is None:
            return True

        def shut() -> None:
            # abort(), unlike stop(), does not wait for a dead server to drain buffers.
            for step in (stream.abort, stream.close):
                try:
                    step()
                except Exception as e:
                    log.debug("closing the microphone: %s", e)

        t = threading.Thread(target=shut, name="mic-close", daemon=True)
        t.start()
        t.join(self._close_timeout)
        return not t.is_alive()

    def _backoff(self, now: float) -> None:
        self._delay = min(max(self._delay * 2, 1.0), self.RETRY_MAX)
        self._next_try = now + self._delay

    def start(self) -> None:
        self._open()

    def keep_alive(self) -> None:
        now = self._clock()
        if self._stream is not None:
            heard = self._last_frame
            if now - (heard if heard is not None else self._opened_at) < self.STALL_SECONDS:
                if heard is not None:
                    self._delay = 0.0
                return
            log.warning("no audio from the microphone for %.0fs; reopening it", self.STALL_SECONDS)
            if not self._close():
                raise MicrophoneWedged("PortAudio hung closing a dead microphone stream")
            if heard is None:
                self._backoff(now)  # it never worked; don't spin reopening it
            else:
                self._next_try = now
        if now < self._next_try:
            return
        try:
            self._open()
        except Exception as e:
            self._backoff(now)
            log.warning("cannot open the microphone (%s); retrying in %.0fs", e, self._delay)

    def stop(self) -> bool:
        """Close the stream; False if PortAudio hung (the caller should not wait on it)."""
        return self._close()

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
