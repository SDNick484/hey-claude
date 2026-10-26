"""User feedback: earcons, desktop notifications and a status file for bars."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np

from hey_claude import config

log = logging.getLogger(__name__)

_RATE = 24000


def _tone(freqs: list[float], dur: float, volume: float) -> np.ndarray:
    """A short chime: consecutive sine notes with a soft attack/decay."""
    parts = []
    for f in freqs:
        t = np.arange(int(_RATE * dur)) / _RATE
        env = np.minimum(1.0, t / 0.01) * np.exp(-t * 9)
        parts.append(np.sin(2 * np.pi * f * t) * env)
    return (np.concatenate(parts) * volume).astype(np.float32)


class Chimes:
    def __init__(self, enabled: bool, volume: float) -> None:
        self.enabled = enabled
        self._sounds = {
            "listen": _tone([660, 990], 0.09, volume),
            "launch": _tone([880, 1320], 0.07, volume),
            "cancel": _tone([660, 440], 0.09, volume),
        }

    def play(self, name: str) -> None:
        if not self.enabled:
            return
        try:
            import sounddevice as sd

            sd.play(self._sounds[name], _RATE)
        except Exception as e:  # audio output trouble should never stop the assistant
            log.debug("chime failed: %s", e)


class Notifier:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled and shutil.which("notify-send") is not None
        if enabled and not self.enabled:
            log.info("notify-send not found; desktop notifications disabled")

    def send(self, title: str, body: str = "", *, urgency: str = "low") -> None:
        if not self.enabled:
            return
        argv = ["notify-send", "--app-name=Hey Claude", f"--urgency={urgency}",
                "--icon=audio-input-microphone", "--expire-time=4000", title]
        if body:
            argv.append(body)
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log.debug("notify-send failed: %s", e)


def status_file() -> Path:
    return config.runtime_dir() / "status.json"


def write_status(state: str, muted: bool, extra: dict | None = None) -> dict:
    status = {"state": state, "muted": muted, "since": time.time(), **(extra or {})}
    path = status_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status))
    tmp.replace(path)
    return status


ICONS = {"idle": "●", "listening": "◉", "followup": "◉", "thinking": "…", "muted": "○", "stopped": "×"}


def waybar(status: dict) -> dict:
    """Format a status dict as a Waybar custom-module JSON object."""
    state = status.get("state", "stopped")
    tooltip = f"Hey Claude: {state}"
    if status.get("last"):
        tooltip += f"\nLast: {status['last']}"
    return {"text": f"{ICONS.get(state, '?')} Claude", "alt": state, "class": state, "tooltip": tooltip}
