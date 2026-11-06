"""The daemon: microphone -> pipeline -> launcher, plus control socket and hotkeys."""

from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from dataclasses import dataclass

import numpy as np

from hey_claude import audio, control, feedback, history, models, speech
from hey_claude.config import Config
from hey_claude.launcher import Launcher
from hey_claude.pipeline import Launch, Pipeline, Settings, State

log = logging.getLogger(__name__)


def ensure_models() -> None:
    missing = models.missing()
    if missing:
        names = ", ".join(m.key for m in missing)
        raise SystemExit(f"speech models missing ({names}); run `hey-claude setup` first")


@dataclass
class Engines:
    spotter: speech.WakeWordSpotter
    vad: speech.VoiceActivity
    asr: speech.Transcriber


def load_engines(cfg: Config) -> Engines:
    ensure_models()
    t = time.monotonic()
    spotter = speech.WakeWordSpotter(cfg.wake.phrases, cfg.wake.threshold, cfg.wake.boost,
                                     threads=1, provider=cfg.asr.provider)
    # min_silence is what ends an utterance; the detector holds "speech" across shorter gaps.
    vad = speech.VoiceActivity(min_silence=cfg.wake.end_silence, max_speech=cfg.wake.max_utterance_seconds)
    asr = speech.Transcriber(threads=cfg.asr.threads, provider=cfg.asr.provider)
    log.info("models loaded in %.1fs", time.monotonic() - t)
    return Engines(spotter, vad, asr)


def settings(cfg: Config) -> Settings:
    return Settings(
        phrases=cfg.wake.phrases,
        projects=cfg.projects,
        wake_enabled=cfg.wake.enabled,
        followup_seconds=cfg.wake.followup_seconds,
        preroll_seconds=cfg.wake.preroll_seconds,
        max_utterance_seconds=cfg.wake.max_utterance_seconds,
    )


class Daemon:
    def __init__(self, cfg: Config, *, dry_run: bool = False) -> None:
        self.cfg = cfg
        # (command, set when applied) from the socket and hotkey threads.
        self.events: queue.Queue[tuple[str, threading.Event | None]] = queue.Queue()
        self.hub = control.Hub()
        self.chimes = feedback.Chimes(cfg.audio.chimes, cfg.audio.chime_volume)
        self.notifier = feedback.Notifier(cfg.notify.enabled)
        self.launcher = Launcher(cfg.launch, cfg.projects, dry_run=dry_run)
        self.last = ""
        self._running = True

        for problem in self.launcher.problems():
            log.warning("%s", problem)
        engines = load_engines(cfg)
        self.pipeline = Pipeline(engines.spotter, engines.vad, engines.asr, settings(cfg),
                                 on_launch=self._on_launch, on_state=self._on_state,
                                 on_cancel=self._on_cancel)
        self._prev_state = self.pipeline.state
        self.mic = audio.Microphone(cfg.audio.device)
        self.server = control.Server(self.hub, self._dispatch)
        self.hotkeys = None

    def _dispatch(self, cmd: str) -> None:
        """Called from socket threads: queue the command and wait until it is applied,
        so the reply carries the new state."""
        done = threading.Event()
        self.events.put((cmd, done))
        done.wait(timeout=5)

    # -- callbacks (all run on the main loop thread) ------------------------------

    def _publish(self) -> None:
        status = {"state": self.pipeline.state.value, "muted": self.pipeline.muted, "last": self.last}
        feedback.write_status(status["state"], status["muted"], {"last": self.last})
        self.hub.publish(status)

    def _on_state(self, state: State) -> None:
        # Chime once when capture starts, not again when a follow-up prompt begins.
        if state == State.LISTENING and self._prev_state in (State.IDLE, State.MUTED):
            self.chimes.play("listen")
            if self.cfg.notify.on_listen:
                self.notifier.send("Listening…")
        self._prev_state = state
        self._publish()

    def _on_cancel(self) -> None:
        self.chimes.play("cancel")

    def _on_launch(self, launch: Launch) -> None:
        req = launch.request
        try:
            outcome = self.launcher.launch(req)
        except Exception as e:
            log.error("launch failed: %s", e)
            self.chimes.play("cancel")
            self.notifier.send("Hey Claude: launch failed", str(e), urgency="normal")
            return
        self.chimes.play("launch")
        self.notifier.send(outcome.summary, req.prompt)
        self.last = req.prompt or "(new session)"
        history.record({"source": launch.source.value, "heard": launch.text, "prompt": req.prompt,
                        "project": req.project, "workdir": outcome.workdir, "summary": outcome.summary})
        self._publish()

    # -- commands from the socket / hotkey threads --------------------------------

    def _handle(self, cmd: str) -> None:
        p = self.pipeline
        if cmd == "mute":
            p.set_muted(True)
        elif cmd == "unmute":
            p.set_muted(False)
        elif cmd == "toggle-mute":
            p.set_muted(not p.muted)
        elif cmd == "ptt-start":
            p.ptt_start()
        elif cmd == "ptt-stop":
            p.ptt_stop()
        elif cmd == "ptt-toggle":
            if p.ptt_active:
                p.ptt_stop()
            else:
                p.ptt_start()
        elif cmd == "cancel":
            p.cancel()
        self._publish()

    def _start_hotkeys(self) -> None:
        backend = self.cfg.ptt.backend
        if backend == "none":
            return
        from hey_claude import hotkey

        ok, why = hotkey.available()
        if not ok:
            msg = f"push-to-talk via evdev unavailable: {why}"
            if backend == "evdev":
                raise SystemExit(msg)
            log.info("%s; bind `hey-claude ptt start` / `ptt stop` in your compositor instead", msg)
            return
        self.hotkeys = hotkey.HotkeyListener(
            self.cfg.ptt.key, self.cfg.ptt.cancel_key,
            on_press=lambda: self.events.put(("ptt-start", None)),
            on_release=lambda: self.events.put(("ptt-stop", None)),
            on_cancel=lambda: self.events.put(("cancel", None)))
        self.hotkeys.start()

    def stop(self, *_args) -> None:
        self._running = False

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.server.start()
        self._start_hotkeys()
        self.mic.start()
        self._publish()
        phrases = " / ".join(f'"{p}"' for p in self.cfg.wake.phrases)
        log.info("ready: say %s%s", phrases, " (muted)" if self.pipeline.muted else "")
        try:
            while self._running:
                while True:
                    try:
                        cmd, done = self.events.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._handle(cmd)
                    finally:
                        if done:
                            done.set()
                frame = self.mic.read(timeout=0.05)
                if frame is not None:
                    self.pipeline.process(frame)
        finally:
            self.mic.stop()
            self.server.stop()
            if self.hotkeys:
                self.hotkeys.stop()
            feedback.write_status("stopped", self.pipeline.muted)


def simulate(cfg: Config, samples: np.ndarray, *, ptt: list[tuple[float, float]] | None = None) -> list[tuple[float, Launch, list[list[str]]]]:
    """Run the pipeline over recorded audio with a dry-run launcher. Returns launches."""
    engines = load_engines(cfg)
    launcher = Launcher(cfg.launch, cfg.projects, dry_run=True)
    results: list[tuple[float, Launch, list[list[str]]]] = []
    pipe: Pipeline

    def on_launch(launch: Launch) -> None:
        outcome = launcher.launch(launch.request)
        results.append((pipe.now, launch, outcome.commands))

    pipe = Pipeline(engines.spotter, engines.vad, engines.asr, settings(cfg), on_launch=on_launch,
                    on_state=lambda s: log.info("%6.2fs  %s", pipe.now, s.value))
    presses = sorted(ptt or [])
    tail = np.zeros(int((cfg.wake.followup_seconds + 2) * speech.SAMPLE_RATE), np.float32)
    for frame in audio.frames_of(np.concatenate([samples, tail])):
        for start, end in presses:
            if pipe.now <= start < pipe.now + speech.FRAME / speech.SAMPLE_RATE:
                pipe.ptt_start()
            if pipe.now <= end < pipe.now + speech.FRAME / speech.SAMPLE_RATE:
                pipe.ptt_stop()
        pipe.process(frame)
    return results
