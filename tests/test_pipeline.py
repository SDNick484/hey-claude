"""State machine tests with scripted fake engines (no models, no audio)."""

import numpy as np

from hey_claude.pipeline import Pipeline, Settings, Source, State
from hey_claude.speech import FRAME, SAMPLE_RATE, Transcript

FPS = SAMPLE_RATE / FRAME  # frames per second


class Script:
    """Per-frame scripted behaviour: which frames are speech and where the wake word fires."""

    def __init__(self):
        self.speech: set[int] = set()
        self.wake: set[int] = set()
        self.pipe = None

    @property
    def frame(self) -> int:
        # The pipeline advances its sample clock before consulting the engines.
        return self.pipe.samples // FRAME - 1

    def speak(self, start: float, end: float):
        self.speech |= set(range(int(start * FPS), int(end * FPS)))

    def wake_at(self, t: float):
        self.wake.add(int(t * FPS))


class FakeSpotter:
    def __init__(self, script):
        self.s = script
        self.resets = 0

    def accept(self, samples):
        return "HEY_CLAUDE" if self.s.frame in self.s.wake else None

    def reset(self):
        self.resets += 1


class FakeVad:
    def __init__(self, script):
        self.s = script

    def accept(self, samples):
        return self.s.frame in self.s.speech

    def reset(self):
        pass


class FakeAsr:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def transcribe(self, samples):
        self.calls.append(len(samples) / SAMPLE_RATE)
        return Transcript(self.texts.pop(0) if self.texts else "")


def make(asr, script=None, **kw):
    script = script or Script()
    launches, states, cancels = [], [], []
    settings = Settings(phrases=["hey claude"], projects={"dotfiles": "~/.dotfiles"}, **kw)
    pipe = Pipeline(FakeSpotter(script), FakeVad(script), asr, settings,
                    on_launch=launches.append, on_state=states.append, on_cancel=lambda: cancels.append(1))
    script.pipe = pipe
    return pipe, script, launches, states, cancels


def run(pipe, seconds):
    frame = np.zeros(FRAME, np.float32)
    for _ in range(int(seconds * FPS)):
        pipe.process(frame)


def test_wake_with_prompt_launches_and_strips_wake_phrase():
    pipe, script, launches, states, _ = make(FakeAsr("Hey Claude, run the tests."))
    script.speak(1.0, 3.0)
    script.wake_at(1.6)
    run(pipe, 5)
    assert [l.request.prompt for l in launches] == ["run the tests."]
    assert launches[0].source == Source.WAKE
    assert states[:3] == [State.LISTENING, State.THINKING, State.IDLE]


def test_preroll_includes_audio_before_detection():
    asr = FakeAsr("Hey Claude, go.")
    pipe, script, *_ = make(asr, preroll_seconds=2.0)
    script.speak(1.0, 2.5)
    script.wake_at(1.8)
    run(pipe, 4)
    # Utterance audio = 1.8 s of pre-roll + capture until speech ends (~2.5 s).
    assert 2.4 <= asr.calls[0] <= 2.7


def test_bare_wake_then_followup_prompt():
    pipe, script, launches, states, _ = make(FakeAsr("Hey Claude.", "open dotfiles and fix git"))
    script.speak(1.0, 1.8)
    script.wake_at(1.7)
    script.speak(3.0, 4.5)
    run(pipe, 6)
    assert State.FOLLOWUP in states
    assert len(launches) == 1
    assert launches[0].request.project == "dotfiles"
    assert launches[0].request.prompt == "fix git"


def test_bare_wake_times_out_to_empty_session():
    pipe, script, launches, *_ = make(FakeAsr("Hey Claude."), followup_seconds=2.0)
    script.speak(1.0, 1.8)
    script.wake_at(1.7)
    run(pipe, 6)
    assert len(launches) == 1 and launches[0].request.prompt == ""


def test_timestamp_fallback_when_wake_not_in_transcript():
    class Asr:
        def transcribe(self, samples):
            # ASR mangled the wake word beyond recognition; timestamps still help.
            return Transcript("Hazel odd. List branches.", tokens=[" Haz", "el", " odd.", " List", " branches."],
                              timestamps=[0.0, 0.2, 0.5, 1.3, 1.6])

    pipe, script, launches, *_ = make(Asr(), preroll_seconds=1.0, detection_lag=0.4)
    script.speak(1.0, 3.0)
    script.wake_at(2.0)  # buffer starts at 1.0, so the hit is 1.0 s in -> cutoff 0.6 s
    run(pipe, 4)
    assert launches[0].request.prompt == "List branches."


def test_no_wake_no_launch():
    pipe, script, launches, *_ = make(FakeAsr())
    script.speak(1.0, 3.0)
    run(pipe, 4)
    assert launches == []


def test_muted_ignores_wake_but_ptt_works():
    asr = FakeAsr("open the logs")
    pipe, script, launches, *_ = make(asr)
    pipe.set_muted(True)
    script.wake_at(1.0)
    run(pipe, 2)
    assert pipe.state == State.MUTED and launches == []
    pipe.ptt_start()
    run(pipe, 1)
    pipe.ptt_stop()
    assert [l.request.prompt for l in launches] == ["open the logs"]
    assert launches[0].source == Source.PTT
    assert pipe.state == State.MUTED


def test_ptt_ignores_silence_and_waits_for_release():
    pipe, script, launches, *_ = make(FakeAsr("write a haiku"))
    pipe.ptt_start()
    run(pipe, 3)  # long pause; push-to-talk never ends on silence
    assert pipe.state == State.LISTENING
    pipe.ptt_stop()
    assert len(launches) == 1


def test_ptt_tap_is_ignored():
    asr = FakeAsr("x")
    pipe, *_rest = make(asr)
    pipe.ptt_start()
    run(pipe, 0.1)
    pipe.ptt_stop()
    assert asr.calls == [] and pipe.state == State.IDLE


def test_ptt_with_nothing_said_cancels():
    pipe, script, launches, states, cancels = make(FakeAsr(""))
    pipe.ptt_start()
    run(pipe, 1)
    pipe.ptt_stop()
    assert launches == [] and cancels == [1]


def test_cancel_during_listening():
    pipe, script, launches, states, cancels = make(FakeAsr("never used"))
    script.speak(1.0, 5.0)
    script.wake_at(1.5)
    run(pipe, 2)
    assert pipe.state == State.LISTENING
    pipe.cancel()
    run(pipe, 4)
    assert launches == [] and cancels == [1] and pipe.state == State.IDLE


def test_max_utterance_cap():
    asr = FakeAsr("Hey Claude, " + "blah " * 50)
    pipe, script, launches, *_ = make(asr, max_utterance_seconds=3.0, preroll_seconds=0.5)
    script.speak(1.0, 20.0)
    script.wake_at(1.5)
    run(pipe, 6)
    assert len(launches) == 1
    assert asr.calls[0] <= 3.1


def test_wake_disabled_starts_muted():
    pipe, *_ = make(FakeAsr(), wake_enabled=False)
    assert pipe.state == State.MUTED
