"""Start (or reuse) a Claude Code session in a terminal or tmux."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from hey_claude import config as cfgmod
from hey_claude.config import LaunchConfig
from hey_claude.transcript import Request

log = logging.getLogger(__name__)

WINDOW_CLASS = "hey-claude"

# name -> builder(workdir, argv, title) -> terminal argv. The class/app-id lets
# compositor rules (sway, Hyprland, i3...) place and focus the window.
TERMINALS: dict[str, Callable[[str, list[str], str], list[str]]] = {
    "kitty": lambda wd, cmd, t: ["kitty", "--class", WINDOW_CLASS, "--title", t, "--directory", wd, *cmd],
    "foot": lambda wd, cmd, t: ["foot", "--app-id", WINDOW_CLASS, "--title", t, f"--working-directory={wd}", *cmd],
    "alacritty": lambda wd, cmd, t: ["alacritty", "--class", WINDOW_CLASS, "--title", t,
                                     "--working-directory", wd, "-e", *cmd],
    "wezterm": lambda wd, cmd, t: ["wezterm", "start", "--class", WINDOW_CLASS, "--cwd", wd, "--", *cmd],
    "ghostty": lambda wd, cmd, t: ["ghostty", f"--title={t}", f"--working-directory={wd}", "-e", *cmd],
    "konsole": lambda wd, cmd, t: ["konsole", "--workdir", wd, "-e", *cmd],
    "gnome-terminal": lambda wd, cmd, t: ["gnome-terminal", f"--title={t}", f"--working-directory={wd}", "--", *cmd],
    "xfce4-terminal": lambda wd, cmd, t: ["xfce4-terminal", "--title", t, "--working-directory", wd, "-x", *cmd],
    "urxvt": lambda wd, cmd, t: ["urxvt", "-name", WINDOW_CLASS, "-title", t, "-cd", wd, "-e", *cmd],
    "st": lambda wd, cmd, t: ["st", "-c", WINDOW_CLASS, "-t", t, "-e", *cmd],
    "xterm": lambda wd, cmd, t: ["xterm", "-class", WINDOW_CLASS, "-T", t, "-e", *cmd],
}
DETECT_ORDER = ["kitty", "foot", "alacritty", "wezterm", "ghostty", "konsole",
                "gnome-terminal", "xfce4-terminal", "urxvt", "st", "xterm"]


def detect_terminal(which: Callable[[str], str | None] = shutil.which) -> str | None:
    env = os.environ.get("TERMINAL")
    if env:
        name = Path(shlex.split(env)[0]).name
        if name in TERMINALS and which(name):
            return name
    return next((t for t in DETECT_ORDER if which(t)), None)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "default"


@dataclass
class Outcome:
    summary: str
    workdir: str
    commands: list[list[str]] = field(default_factory=list)


class Launcher:
    def __init__(self, cfg: LaunchConfig, projects: dict[str, str], *, dry_run: bool = False,
                 which: Callable[[str], str | None] = shutil.which,
                 spawn: Callable[[list[str], str], None] | None = None,
                 run: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> None:
        self.cfg, self.projects, self.dry_run, self.which = cfg, projects, dry_run, which
        self._spawn = spawn or self._default_spawn
        self._run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
        self._commands: list[list[str]] = []

    # -- process helpers ---------------------------------------------------------

    @staticmethod
    def _default_spawn(argv: list[str], cwd: str) -> None:
        subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    def _start(self, argv: list[str], cwd: str) -> None:
        self._commands.append(argv)
        log.info("exec: %s", shlex.join(argv))
        if not self.dry_run:
            self._spawn(argv, cwd)

    def _check(self, argv: list[str]) -> subprocess.CompletedProcess:
        self._commands.append(argv)
        if self.dry_run:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return self._run(argv)

    # -- public ------------------------------------------------------------------

    def workdir(self, request: Request) -> str:
        raw = self.projects.get(request.project, self.cfg.workdir) if request.project else self.cfg.workdir
        path = cfgmod.expand(raw)
        if not path.is_dir():
            log.warning("workdir %s does not exist; using home", path)
            path = Path.home()
        return str(path)

    def claude_argv(self, prompt: str) -> list[str]:
        return [self.cfg.claude, *self.cfg.claude_args, *([prompt] if prompt else [])]

    def terminal_argv(self, workdir: str, cmd: list[str], title: str) -> list[str]:
        name = self.cfg.terminal
        if name == "auto":
            name = detect_terminal(self.which) or ""
        if name not in TERMINALS:
            raise RuntimeError(
                f"no supported terminal found (terminal = {self.cfg.terminal!r}); "
                f"set [launch] terminal to one of: {', '.join(DETECT_ORDER)}")
        return TERMINALS[name](workdir, cmd, title)

    def launch(self, request: Request) -> Outcome:
        self._commands = []
        wd = self.workdir(request)
        where = request.project or Path(wd).name or wd
        mode = self.cfg.mode
        if mode == "terminal":
            summary = self._launch_terminal(request, wd)
        elif mode == "tmux":
            summary = self._launch_tmux(request, wd)
        elif mode == "command":
            summary = self._launch_command(request, wd)
        else:
            raise RuntimeError(f"unknown [launch] mode {mode!r} (terminal, tmux or command)")
        return Outcome(summary=f"{summary} in {where}", workdir=wd, commands=self._commands)

    # -- modes -------------------------------------------------------------------

    def _launch_terminal(self, request: Request, wd: str) -> str:
        self._start(self.terminal_argv(wd, self.claude_argv(request.prompt), "Claude Code"), wd)
        return "Opened Claude Code"

    def _launch_command(self, request: Request, wd: str) -> str:
        if not self.cfg.command:
            raise RuntimeError("[launch] mode = \"command\" needs a `command` argv template")
        values = {"claude": self.cfg.claude, "workdir": wd, "prompt": request.prompt}
        argv = []
        for part in self.cfg.command:
            if part == "{prompt}" and not request.prompt:
                continue  # drop an empty prompt argument entirely
            argv.append(part.format(**values))
        self._start(argv, wd)
        return "Launched"

    def _launch_tmux(self, request: Request, wd: str) -> str:
        session = self.cfg.tmux_session
        if request.project:
            session = f"{session}-{slug(request.project)}"
        target = f"={session}"
        exists = self._check(["tmux", "has-session", "-t", target]).returncode == 0

        if not exists:
            # tmux runs the command through the shell, so quote it.
            self._start(["tmux", "new-session", "-d", "-s", session, "-c", wd,
                         shlex.join(self.claude_argv(request.prompt))], wd)
            summary = "Started Claude Code"
        else:
            if request.prompt:
                self._start(["tmux", "send-keys", "-t", f"{target}:", "-l", request.prompt], wd)
                if not self.dry_run:
                    # Let the TUI take the text as typed input before submitting it.
                    time.sleep(0.15)
                self._start(["tmux", "send-keys", "-t", f"{target}:", "Enter"], wd)
                summary = "Sent to Claude Code"
            else:
                summary = "Focused Claude Code"

        attached = exists and bool(self._check(["tmux", "list-clients", "-t", target]).stdout.strip())
        if attached:
            if self.cfg.focus_command:
                self._start(list(self.cfg.focus_command), wd)
        else:
            # New sessions are created detached, so show them in a terminal window.
            self._start(self.terminal_argv(wd, ["tmux", "attach-session", "-t", target], "Claude Code"), wd)
        return summary

    def problems(self) -> list[str]:
        """Things that will make launching fail, for `hey-claude doctor`."""
        out = []
        if not self.which(self.cfg.claude):
            out.append(f"`{self.cfg.claude}` (Claude Code CLI) is not on PATH")
        if self.cfg.mode in ("terminal", "tmux"):
            if self.cfg.terminal == "auto" and not detect_terminal(self.which):
                out.append("no supported terminal emulator found")
            elif self.cfg.terminal != "auto" and not self.which(self.cfg.terminal):
                out.append(f"terminal {self.cfg.terminal!r} is not on PATH")
        if self.cfg.mode == "tmux" and not self.which("tmux"):
            out.append("tmux is not installed")
        return out
