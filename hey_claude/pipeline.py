"""The listening state machine: wake word / push-to-talk -> utterance -> launch request.

It is driven one audio frame at a time and keeps time by counting samples, so the
same code runs live, over a WAV file (`hey-claude simulate`) and in unit tests.
"""

from __future__ import annotations

import collections
import enum
import logging
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from hey_claude import transcript
from hey_claude.speech import FRAME, SAMPLE_RATE, Transcript

log = logging.getLogger(__name__)


class State(str, enum.Enum):
    IDLE = "idle"            # waiting for the wake word
    LISTENING = "listening"  # capturing an utterance
    FOLLOWUP = "followup"    # heard a bare "Hey Claude", waiting for the prompt
    THINKING = "thinking"    # transcribing
    MUTED = "muted"          # wake word off (push-to-talk still works)


class Source(str, enum.Enum):
    WAKE = "wake"
    PTT = "ptt"


class Spotter(Protocol):
    def accept(self, samples: np.ndarray) -> str | None: ...
    def reset(self) -> None: ...


class Vad(Protocol):
    def accept(self, samples: np.ndarray) -> bool: ...
    def reset(self) -> None: ...


class Asr(Protocol):
    def transcribe(self, samples: np.ndarray) -> Transcript: ...


@dataclass
class Settings:
    phrases: list[str]
    projects: dict[str, str]
    wake_enabled: bool = True
    followup_seconds: float = 3.0
    preroll_seconds: float = 2.5
    max_utterance_seconds: float = 30.0
    # The KWS model reports the keyword ~0.4 s after it ends. If the transcript does
    # not visibly start with the wake phrase, drop tokens older than this before the hit.
    detection_lag: float = 0.6
    # Push-to-talk presses shorter than this are treated as accidental taps.
    min_ptt_seconds: float = 0.3


@dataclass
class Launch:
    request: transcript.Request
    source: Source
    text: str  # raw transcript, for history/debugging


class Pipeline:
    def __init__(self, spotter: Spotter, vad: Vad, asr: Asr, settings: Settings, *,
                 on_launch: Callable[[Launch], None],
                 on_state: Callable[[State], None] = lambda s: None,
                 on_cancel: Callable[[], None] = lambda: None) -> None:
        self.spotter, self.vad, self.asr, self.s = spotter, vad, asr, settings
        self.on_launch, self.on_state, self.on_cancel = on_launch, on_state, on_cancel
        self.samples = 0  # running clock
        self.muted = not settings.wake_enabled
        self.state = State.MUTED if self.muted else State.IDLE
        self._preroll: collections.deque[np.ndarray] = collections.deque(
            maxlen=max(1, int(settings.preroll_seconds * SAMPLE_RATE / FRAME)))
        self._buf: list[np.ndarray] = []
        self._source = Source.WAKE
        self._start = 0          # sample index where _buf begins
        self._hit_at = 0         # sample index of the wake detection
        self._speech_seen = False
        self._deadline = 0
        self._followup = False   # current capture is the follow-up after a bare wake
        self._ptt_started = 0

    # -- helpers -----------------------------------------------------------------

    @property
    def now(self) -> float:
        return self.samples / SAMPLE_RATE

    @property
    def ptt_active(self) -> bool:
        return self.state == State.LISTENING and self._source == Source.PTT

    def _set(self, state: State) -> None:
        if state != self.state:
            self.state = state
            self.on_state(state)

    def _rest_state(self) -> State:
        return State.MUTED if self.muted else State.IDLE

    def _secs(self, seconds: float) -> int:
        return int(seconds * SAMPLE_RATE)

    def _begin(self, source: Source, preroll_frames: int | None = None) -> None:
        frames = list(self._preroll)
        if preroll_frames is not None:
            frames = frames[-preroll_frames:] if preroll_frames else []
        self._buf = frames
        self._start = self.samples - sum(len(f) for f in frames)
        self._source = source
        self._speech_seen = False
        self._set(State.LISTENING)

    # -- events ------------------------------------------------------------------

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        if self.state in (State.IDLE, State.MUTED):
            self._set(self._rest_state())
        if not muted:
            self.spotter.reset()

    def ptt_start(self) -> None:
        if self.ptt_active:
            return
        self._followup = False
        # A little pre-roll catches the first syllable if speech began with the keypress.
        self._begin(Source.PTT, preroll_frames=max(1, self._secs(0.25) // FRAME))
        self._ptt_started = self.samples

    def ptt_stop(self) -> None:
        if not self.ptt_active:
            return
        if self.samples - self._ptt_started < self._secs(self.s.min_ptt_seconds):
            log.debug("push-to-talk tap ignored")
            self._reset_to_rest()
            return
        self._finish()

    def cancel(self) -> None:
        if self.state in (State.LISTENING, State.FOLLOWUP):
            self._reset_to_rest()
            self.on_cancel()

    # -- audio -------------------------------------------------------------------

    def process(self, frame: np.ndarray) -> None:
        self.samples += len(frame)
        speaking = self.vad.accept(frame)
        # The spotter hears every frame (it is tiny) so its streaming context stays
        # continuous; restarting it mid-speech makes the next detection less reliable.
        # Hits outside IDLE are ignored.
        heard_wake = not self.muted and bool(self.spotter.accept(frame))

        if self.state in (State.IDLE, State.MUTED):
            self._preroll.append(frame)
            if self.state == State.IDLE and heard_wake:
                log.info("wake word at %.2fs", self.now)
                self._hit_at = self.samples
                self._followup = False
                self._begin(Source.WAKE)
                self._speech_seen = speaking
                self._deadline = self.samples + self._secs(self.s.followup_seconds)
            return

        if self.state == State.FOLLOWUP:
            self._preroll.append(frame)
            if speaking:
                self._followup = True
                self._begin(Source.WAKE, preroll_frames=max(1, self._secs(0.4) // FRAME))
                self._speech_seen = True
            elif self.samples >= self._deadline:
                self._emit(transcript.Request(prompt=""), Source.WAKE, "")
            return

        if self.state != State.LISTENING:
            return

        self._buf.append(frame)
        if self._source == Source.PTT:
            if self.samples - self._start >= self._secs(self.s.max_utterance_seconds):
                self._finish()
            return

        if speaking:
            self._speech_seen = True
        too_long = self.samples - self._start >= self._secs(self.s.max_utterance_seconds)
        ended = self._speech_seen and not speaking
        silent_too_long = not self._speech_seen and self.samples >= self._deadline
        if ended or too_long or silent_too_long:
            self._finish()

    # -- completion --------------------------------------------------------------

    def _reset_to_rest(self) -> None:
        self._buf = []
        self._preroll.clear()
        self._set(self._rest_state())

    def _finish(self) -> None:
        audio = np.concatenate(self._buf) if self._buf else np.zeros(0, np.float32)
        source, followup = self._source, self._followup
        self._buf = []
        self._set(State.THINKING)
        result = self.asr.transcribe(audio)
        log.info("heard (%s): %r", source.value, result.text)

        text = result.text
        if source == Source.WAKE and not followup:
            rest, found = transcript.strip_wake(text, self.s.phrases, loose=True)
            if not found and result.tokens:
                # Fall back to timing: keep only what was said after the wake word.
                cutoff = (self._hit_at - self._start) / SAMPLE_RATE - self.s.detection_lag
                rest = result.text_after(max(0.0, cutoff))
            request = transcript.parse(rest, phrases=[], projects=self.s.projects)
        else:
            request = transcript.parse(text, phrases=self.s.phrases, projects=self.s.projects)

        if source == Source.PTT and not request.prompt and request.project is None:
            log.info("push-to-talk: nothing heard")
            self._reset_to_rest()
            self.on_cancel()
            return
        if source == Source.WAKE and not followup and not request.prompt and request.project is None:
            # Bare "Hey Claude": give the speaker a moment to continue.
            self._preroll.clear()
            self._deadline = self.samples + self._secs(self.s.followup_seconds)
            self._set(State.FOLLOWUP)
            return
        self._emit(request, source, text)

    def _emit(self, request: transcript.Request, source: Source, text: str) -> None:
        self._preroll.clear()
        self._set(self._rest_state())
        self.on_launch(Launch(request=request, source=source, text=text))
