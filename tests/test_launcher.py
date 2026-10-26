import subprocess

import pytest

from hey_claude.config import LaunchConfig
from hey_claude.launcher import Launcher, detect_terminal, slug
from hey_claude.transcript import Request


def which_only(*names):
    return lambda n: f"/usr/bin/{n}" if n in names else None


@pytest.fixture
def proj(tmp_path):
    d = tmp_path / "dotfiles"
    d.mkdir()
    return {"dotfiles": str(d)}


def test_detect_terminal_prefers_env(monkeypatch):
    monkeypatch.setenv("TERMINAL", "alacritty")
    assert detect_terminal(which_only("kitty", "alacritty")) == "alacritty"
    monkeypatch.setenv("TERMINAL", "/opt/weird-term")
    assert detect_terminal(which_only("kitty", "foot")) == "kitty"
    monkeypatch.delenv("TERMINAL")
    assert detect_terminal(which_only("xterm")) == "xterm"
    assert detect_terminal(which_only()) is None


def test_terminal_mode_with_prompt_and_project(proj):
    cfg = LaunchConfig(terminal="foot", claude_args=["--model", "opus"])
    out = Launcher(cfg, proj, dry_run=True).launch(Request("fix git", "dotfiles"))
    assert out.commands == [["foot", "--app-id", "hey-claude", "--title", "Claude Code",
                             f"--working-directory={proj['dotfiles']}", "claude", "--model", "opus", "fix git"]]
    assert out.summary == "Opened Claude Code in dotfiles"


def test_bare_launch_has_no_prompt_argument(tmp_path):
    cfg = LaunchConfig(terminal="kitty", workdir=str(tmp_path))
    out = Launcher(cfg, {}, dry_run=True).launch(Request(""))
    assert out.commands[0][-1] == "claude"


def test_missing_workdir_falls_back_to_home(tmp_path):
    cfg = LaunchConfig(terminal="kitty", workdir=str(tmp_path / "nope"))
    launcher = Launcher(cfg, {}, dry_run=True)
    assert launcher.workdir(Request("x")) == str(__import__("pathlib").Path.home())


def test_no_terminal_is_a_clear_error():
    launcher = Launcher(LaunchConfig(), {}, dry_run=True, which=which_only())
    with pytest.raises(RuntimeError, match="no supported terminal"):
        launcher.launch(Request("x"))


def test_command_mode_template(tmp_path):
    cfg = LaunchConfig(mode="command", workdir=str(tmp_path), command=["code", "{workdir}", "{prompt}"])
    l = Launcher(cfg, {}, dry_run=True)
    assert l.launch(Request("hi")).commands == [["code", str(tmp_path), "hi"]]
    assert l.launch(Request("")).commands == [["code", str(tmp_path)]]


class FakeTmux:
    def __init__(self, exists: bool, attached: bool):
        self.exists, self.attached = exists, attached
        self.spawned = []

    def run(self, argv):
        if argv[1] == "has-session":
            return subprocess.CompletedProcess(argv, 0 if self.exists else 1, "", "")
        if argv[1] == "list-clients":
            return subprocess.CompletedProcess(argv, 0, "/dev/pts/3: 0 [80x24]\n" if self.attached else "", "")
        raise AssertionError(argv)

    def spawn(self, argv, cwd):
        self.spawned.append(argv)


def tmux_launcher(fake, proj, **kw):
    cfg = LaunchConfig(mode="tmux", terminal="kitty", **kw)
    return Launcher(cfg, proj, run=fake.run, spawn=fake.spawn, which=which_only("kitty", "tmux"))


def test_tmux_new_session_is_created_and_attached(proj):
    fake = FakeTmux(exists=False, attached=False)
    out = tmux_launcher(fake, proj).launch(Request("it's broken", "dotfiles"))
    new, attach = fake.spawned
    assert new[:5] == ["tmux", "new-session", "-d", "-s", "hey-claude-dotfiles"]
    assert new[-1] == "claude 'it'\"'\"'s broken'"  # shell-quoted for tmux
    assert attach[-4:] == ["tmux", "attach-session", "-t", "=hey-claude-dotfiles"]
    assert out.summary.startswith("Started")


def test_tmux_existing_attached_session_gets_typed_prompt(proj, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = FakeTmux(exists=True, attached=True)
    out = tmux_launcher(fake, proj, focus_command=["swaymsg", "[app_id=hey-claude] focus"]).launch(
        Request("now add tests"))
    assert fake.spawned == [
        ["tmux", "send-keys", "-t", "=hey-claude:", "-l", "now add tests"],
        ["tmux", "send-keys", "-t", "=hey-claude:", "Enter"],
        ["swaymsg", "[app_id=hey-claude] focus"],
    ]
    assert out.summary.startswith("Sent")


def test_tmux_existing_detached_session_is_reattached(proj):
    fake = FakeTmux(exists=True, attached=False)
    tmux_launcher(fake, proj).launch(Request(""))
    assert fake.spawned == [["kitty", "--class", "hey-claude", "--title", "Claude Code", "--directory",
                             str(__import__("pathlib").Path.home()), "tmux", "attach-session", "-t", "=hey-claude"]]


def test_slug():
    assert slug("Web API") == "web-api"
    assert slug("!!!") == "default"


def test_problems():
    l = Launcher(LaunchConfig(mode="tmux"), {}, which=which_only("kitty"))
    assert l.problems() == ["`claude` (Claude Code CLI) is not on PATH", "tmux is not installed"]
