import threading

import numpy as np
import pytest

from hey_claude.audio import Microphone, MicrophoneWedged
from hey_claude.speech import FRAME


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeStream:
    def __init__(self, callback, *, hang_on_close: bool = False) -> None:
        self.callback = callback
        self.hang_on_close = hang_on_close
        self.started = self.closed = False
        self.release = threading.Event()

    def deliver(self) -> None:
        self.callback(np.zeros((FRAME, 1), np.float32), FRAME, None, None)

    def start(self) -> None:
        self.started = True

    def abort(self) -> None:
        if self.hang_on_close:
            self.release.wait()

    def close(self) -> None:
        self.closed = True


class Opener:
    """Stands in for PortAudio: fails while `down` is set, otherwise hands out FakeStreams."""

    def __init__(self, *, down: bool = False, hang_on_close: bool = False) -> None:
        self.down = down
        self.hang_on_close = hang_on_close
        self.streams: list[FakeStream] = []

    def __call__(self, device, callback) -> FakeStream:
        if self.down:
            raise RuntimeError("Error opening InputStream: Unanticipated host error")
        stream = FakeStream(callback, hang_on_close=self.hang_on_close)
        self.streams.append(stream)
        return stream


def mic_with(opener: Opener, clock: Clock) -> Microphone:
    return Microphone(open_stream=opener, clock=clock, close_timeout=0.2)


def test_keep_alive_opens_and_starts_the_stream():
    opener, clock = Opener(), Clock()
    mic = mic_with(opener, clock)
    mic.keep_alive()
    assert len(opener.streams) == 1 and opener.streams[0].started
    opener.streams[0].deliver()
    assert mic.read(timeout=0) is not None


def test_audio_server_down_at_startup_is_retried_with_backoff():
    # At login the daemon can start while PipeWire is still being (re)started.
    opener, clock = Opener(down=True), Clock()
    mic = mic_with(opener, clock)
    mic.keep_alive()  # must not raise
    assert opener.streams == []

    opener.down = False
    clock.t = 0.5
    mic.keep_alive()
    assert opener.streams == [], "retried before the backoff elapsed"

    clock.t = 1.0
    mic.keep_alive()
    assert len(opener.streams) == 1


def test_backoff_grows_while_the_server_stays_down():
    opener, clock = Opener(down=True), Clock()
    mic = mic_with(opener, clock)
    attempts = []
    real_call = opener.__call__

    def counting(device, callback):
        attempts.append(clock.t)
        return real_call(device, callback)

    mic._open_stream = counting
    while clock.t < 40:
        mic.keep_alive()
        clock.t += 0.25
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    assert gaps == sorted(gaps), "backoff never shrinks while failing"
    assert gaps[0] == pytest.approx(1.0) and max(gaps) <= Microphone.RETRY_MAX


def test_a_stream_that_goes_silent_is_replaced():
    # After a PipeWire restart PortAudio keeps the stream "active" but delivers nothing.
    opener, clock = Opener(), Clock()
    mic = mic_with(opener, clock)
    mic.keep_alive()
    first = opener.streams[0]
    first.deliver()

    clock.t = Microphone.STALL_SECONDS - 0.5
    mic.keep_alive()
    assert len(opener.streams) == 1, "replaced a stream that was still within the grace period"

    clock.t = Microphone.STALL_SECONDS + 0.5
    mic.keep_alive()
    assert first.closed
    assert len(opener.streams) == 2 and opener.streams[1].started


def test_frames_keep_a_stream_alive():
    opener, clock = Opener(), Clock()
    mic = mic_with(opener, clock)
    mic.keep_alive()
    for _ in range(20):
        clock.t += 1.0
        opener.streams[0].deliver()
        mic.keep_alive()
    assert len(opener.streams) == 1


def test_a_hung_close_is_reported_instead_of_blocking():
    opener, clock = Opener(hang_on_close=True), Clock()
    mic = mic_with(opener, clock)
    mic.keep_alive()
    clock.t = Microphone.STALL_SECONDS + 1
    try:
        with pytest.raises(MicrophoneWedged):
            mic.keep_alive()
    finally:
        opener.streams[0].release.set()


def test_stop_returns_even_if_portaudio_hangs():
    # This is what made SIGTERM appear to do nothing.
    opener, clock = Opener(hang_on_close=True), Clock()
    mic = mic_with(opener, clock)
    mic.keep_alive()
    try:
        assert mic.stop() is False
    finally:
        opener.streams[0].release.set()


def test_stop_reports_a_clean_close():
    opener, clock = Opener(), Clock()
    mic = mic_with(opener, clock)
    mic.keep_alive()
    assert mic.stop() is True
    assert opener.streams[0].closed
