"""Configuration: XDG paths plus a TOML file merged over dataclass defaults."""

from __future__ import annotations

import dataclasses
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP = "hey-claude"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / APP
    # No runtime dir (e.g. a bare TTY): fall back to a per-user dir under state.
    return state_dir() / "run"


def config_path() -> Path:
    return config_dir() / "config.toml"


def models_dir() -> Path:
    return data_dir() / "models"


def socket_path() -> Path:
    return runtime_dir() / "control.sock"


@dataclass
class WakeConfig:
    enabled: bool = True
    # Each phrase is tokenised with the KWS model's BPE vocabulary at startup.
    phrases: list[str] = field(default_factory=lambda: ["hey claude"])
    # Lower threshold = more sensitive (more false accepts); see `hey-claude calibrate`.
    threshold: float = 0.25
    boost: float = 1.5
    # After a bare "Hey Claude", how long to wait for the prompt before launching empty.
    followup_seconds: float = 3.0
    # Silence that ends an utterance.
    end_silence: float = 0.8
    # Audio kept from before the detection so the start of the utterance is not lost.
    preroll_seconds: float = 2.5
    max_utterance_seconds: float = 30.0


@dataclass
class PttConfig:
    # auto: use evdev when /dev/input is readable, otherwise rely on `hey-claude ptt` bindings.
    backend: str = "auto"
    key: str = "KEY_RIGHTALT"
    cancel_key: str = "KEY_ESC"


@dataclass
class AudioConfig:
    # sounddevice input device (name substring or index); empty means the system default.
    device: str = ""
    chimes: bool = True
    chime_volume: float = 0.25


@dataclass
class AsrConfig:
    threads: int = 4
    provider: str = "cpu"


@dataclass
class LaunchConfig:
    # terminal: new terminal window per launch
    # tmux:     one persistent tmux session; later prompts are typed into it
    # command:  run `command` (argv template) yourself
    mode: str = "terminal"
    terminal: str = "auto"
    workdir: str = "~"
    claude: str = "claude"
    claude_args: list[str] = field(default_factory=list)
    tmux_session: str = "hey-claude"
    # argv template for mode = "command"; placeholders: {claude} {workdir} {prompt}
    command: list[str] = field(default_factory=list)
    # Optional argv run after a prompt is sent to an already-attached tmux session,
    # e.g. ["swaymsg", "[app_id=hey-claude] focus"].
    focus_command: list[str] = field(default_factory=list)


@dataclass
class NotifyConfig:
    enabled: bool = True
    # Show a notification when listening starts (the chime usually suffices).
    on_listen: bool = False


@dataclass
class Config:
    wake: WakeConfig = field(default_factory=WakeConfig)
    ptt: PttConfig = field(default_factory=PttConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    launch: LaunchConfig = field(default_factory=LaunchConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    # Spoken project name -> directory. "Hey Claude, in <name>, ..." opens there.
    projects: dict[str, str] = field(default_factory=dict)


def _apply(section: Any, values: dict[str, Any], where: str) -> None:
    known = {f.name: f for f in dataclasses.fields(section)}
    for key, value in values.items():
        if key not in known:
            log.warning("config: unknown key %s.%s (ignored)", where, key)
            continue
        current = getattr(section, key)
        if isinstance(current, bool):
            ok = isinstance(value, bool)
        elif isinstance(current, float):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            value = float(value) if ok else value
        elif isinstance(current, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(current, list):
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        else:
            ok = isinstance(value, type(current))
        if not ok:
            raise ValueError(f"config: {where}.{key} has the wrong type: {value!r}")
        setattr(section, key, value)


def from_dict(data: dict[str, Any]) -> Config:
    cfg = Config()
    for name, values in data.items():
        if name == "projects":
            if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
                raise ValueError("config: [projects] must map names to directory strings")
            cfg.projects = {k.lower(): v for k, v in values.items()}
        elif name in {f.name for f in dataclasses.fields(Config)}:
            if not isinstance(values, dict):
                raise ValueError(f"config: [{name}] must be a table")
            _apply(getattr(cfg, name), values, name)
        else:
            log.warning("config: unknown section [%s] (ignored)", name)
    return cfg


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        return from_dict(tomllib.load(f))


def expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def set_wake_tuning(threshold: float, boost: float, path: Path | None = None) -> Path:
    """Write calibrated wake values into the config file, preserving everything else.

    tomllib cannot write, so this edits the [wake] table textually: replace existing
    keys if present, otherwise add them under [wake] (creating the table if needed).
    """
    import re

    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    values = {"threshold": f"{threshold:.2f}", "boost": f"{boost:.2f}"}

    m = re.search(r"^\[wake\]\s*$", text, re.M)
    if not m:
        body = "".join(f"{k} = {v}\n" for k, v in values.items())
        text = (text.rstrip() + "\n\n" if text.strip() else "") + "[wake]\n" + body
    else:
        end = re.search(r"^\[", text[m.end():], re.M)
        stop = m.end() + end.start() if end else len(text)
        section = text[m.end():stop]
        for key, value in values.items():
            pat = re.compile(rf"^{key}\s*=.*$", re.M)
            if pat.search(section):
                section = pat.sub(f"{key} = {value}", section)
            else:
                section = section.rstrip("\n") + f"\n{key} = {value}\n"
        if not section.endswith("\n\n") and end:
            section = section.rstrip("\n") + "\n\n"
        text = text[: m.end()] + section + text[stop:]
    path.write_text(text)
    return path
