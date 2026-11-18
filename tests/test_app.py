"""Daemon loop: the microphone is open only while something needs it."""

import pytest

from hey_claude import app, audio, config
from hey_claude.pipeline import State
from hey_claude.speech import Transcript

from test_audio import Clock, Opener


class Engine:
    def accept(self, samples):
        return None

    def reset(self):
        pass

    def transcribe(self, samples):
        return Transcript("")


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(app, "load_engines", lambda cfg: app.Engines(Engine(), Engine(), Engine()))
    cfg = config.Config()
    cfg.audio.chimes = False
    cfg.notify.enabled = False
    d = app.Daemon(cfg, dry_run=True)
    d.opener = Opener()
    d.mic = audio.Microphone(open_stream=d.opener, clock=Clock(), close_timeout=0.2)
    return d


def tick(d, *commands):
    for cmd in commands:
        d.events.put((cmd, None))
    d._tick()


def test_wake_word_is_off_by_default_and_the_mic_stays_closed(daemon):
    assert daemon.pipeline.state == State.MUTED
    tick(daemon)
    assert daemon.opener.streams == []


def test_push_to_talk_opens_the_mic_and_closes_it_afterwards(daemon):
    tick(daemon, "ptt-start")
    tick(daemon)
    assert daemon.mic.is_open
    stream = daemon.opener.streams[0]
    for _ in range(20):
        stream.deliver()
        tick(daemon)
    tick(daemon, "ptt-stop")
    assert daemon.pipeline.state == State.MUTED
    tick(daemon, "ptt-start")  # the idle tick closes the mic before taking the next press
    assert stream.closed and not daemon.mic.is_open
    tick(daemon)
    assert daemon.mic.is_open and len(daemon.opener.streams) == 2


def test_unmuting_keeps_the_mic_open_for_the_wake_word(daemon):
    tick(daemon, "unmute")
    for _ in range(3):
        tick(daemon)
    assert daemon.mic.is_open and len(daemon.opener.streams) == 1
    tick(daemon, "mute")
    tick(daemon)
    assert not daemon.mic.is_open


def test_a_failed_open_does_not_delay_the_next_press(daemon):
    daemon.opener.down = True
    tick(daemon, "ptt-start")
    tick(daemon)  # fails; backoff armed
    tick(daemon, "ptt-stop")
    daemon.opener.down = False
    tick(daemon, "ptt-start")
    tick(daemon)
    assert daemon.mic.is_open
