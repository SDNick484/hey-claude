# hey-claude

A voice-activated launcher for [Claude Code](https://docs.claude.com/en/docs/claude-code) on Linux.
Say **"Hey Claude"** and a Claude Code session opens. Keep talking (*"Hey Claude, summarize the
last five commits"*) and what you said becomes the opening prompt. Or hold a key to talk.

Inspired by [littlemelon77/hey-claude](https://github.com/littlemelon77/hey-claude) for macOS. It
uses the same on-device speech stack (sherpa-onnx with a zipformer keyword spotter, Silero VAD and
NVIDIA Parakeet TDT), rebuilt in Python for Linux desktops. All speech processing is local; no
audio leaves your machine. Claude Code itself talks to Anthropic as usual once it's running.

```
mic ─▶ wake word (KWS zipformer) ─▶ VAD end-of-speech ─▶ Parakeet STT ─▶ route ─▶ claude
            ▲                                                              │
 push-to-talk (evdev or compositor binding)          terminal window / tmux session / your command
```

## What it does

| | |
|---|---|
| **Wake word** | "Hey Claude" opens a session. Pre-roll audio is kept, so you can run straight on into the prompt without pausing. |
| **Prompt passthrough** | "Hey Claude, *why is CI red?*" starts `claude "why is CI red?"`. |
| **Follow-up** | Say "Hey Claude", pause, then speak: you get up to 3 s to start talking. Silence opens an empty session. |
| **Projects** | "Hey Claude, *in dotfiles,* clean up my zshrc" opens in `~/.dotfiles` (see `[projects]`). |
| **Push-to-talk** | Hold Right Alt (evdev), or bind `hey-claude ptt start/stop` in sway or Hyprland. Pauses never cut you off. Esc (evdev) or `hey-claude cancel` aborts. |
| **Launch targets** | A new terminal per launch (kitty, foot, alacritty, wezterm, ghostty, konsole, gnome-terminal, xfce4-terminal, urxvt, st, xterm), **or** one persistent **tmux** session per project that later prompts are typed into, **or** any command you like. |
| **Status** | Earcons, `notify-send` notifications, a live Waybar module, `hey-claude status`. |
| **Tuning** | `hey-claude calibrate` records your voice and picks the wake threshold for your accent and mic. |
| **History** | `hey-claude history` lists recent launches. |

## Install on Gentoo

Runtime needs: Python ≥ 3.11 on glibc (the sherpa-onnx wheels are manylinux; musl profiles are
not supported), PortAudio, and the Claude Code CLI.

```sh
# System packages (tmux and libnotify are optional)
emerge --ask media-libs/portaudio dev-python/pipx app-misc/tmux x11-libs/libnotify

# Audio goes through PortAudio's ALSA backend, so on PipeWire make sure the ALSA
# plugin is built:  media-video/pipewire USE="sound-server pipewire-alsa"

# Claude Code, if you don't have it yet
curl -fsSL https://claude.ai/install.sh | bash

# hey-claude itself, isolated from the system Python (PEP 668)
git clone https://github.com/SDNick484/hey-claude && cd hey-claude
pipx install .
pipx inject hey-claude evdev        # optional: global push-to-talk via /dev/input
# If sherpa-onnx has no wheel for your newest Python yet:  pipx install --python python3.13 .

hey-claude setup       # downloads ~500 MB of models to ~/.local/share/hey-claude/models
hey-claude doctor      # checks mic, models, terminal, claude, push-to-talk
hey-claude calibrate   # ~1 min: say "Hey Claude" 5x, then read a paragraph aloud
hey-claude run         # foreground; Ctrl-C to stop
```

### Starting it automatically

- **OpenRC (the default Gentoo profile):** start it from your session, not as a system service,
  because it needs your audio and display. Use XDG autostart
  (`cp contrib/hey-claude.desktop ~/.config/autostart/`), or `exec hey-claude run` in sway /
  `exec-once = hey-claude run` in Hyprland.
- **systemd profile:** `contrib/systemd/hey-claude.service` is a user unit bound to
  `graphical-session.target`; installation steps are in the file header.

### Push-to-talk permissions

The evdev backend reads `/dev/input/event*`, which requires `gpasswd -a $USER input` (log out
and back in). Members of that group can read **all** keyboard input. hey-claude only reacts to
the configured key codes and records nothing, but if you'd rather not grant this, set
`[ptt] backend = "none"` and use compositor bindings instead. See
[contrib/bindings.md](contrib/bindings.md) for sway, i3, Hyprland, KDE and GNOME.

Right Alt is AltGr on many international layouts. If you use AltGr, set `key = "KEY_RIGHTCTRL"`
or another key.

## Configuration

`hey-claude config --init` writes a commented `~/.config/hey-claude/config.toml`
([defaults](hey_claude/config.example.toml)). Typical changes:

```toml
[launch]
mode = "tmux"            # one long-lived session per project; follow-up prompts are typed into it
terminal = "foot"
workdir = "~/src"
claude_args = ["--model", "opus"]
focus_command = ["swaymsg", "[app_id=hey-claude] focus"]

[projects]
"dotfiles" = "~/.dotfiles"
"hey claude" = "~/src/hey-claude"   # say it the way you'd say it out loud

[wake]
phrases = ["hey claude", "hi claude"]   # add variants your accent produces
```

`mode = "command"` runs any argv template instead, with the placeholders `{claude}`,
`{workdir}` and `{prompt}`. For example, to open VS Code: `command = ["code", "{workdir}"]`.

## Commands

| Command | |
|---|---|
| `hey-claude run [--dry-run]` | Run the listener (`--dry-run` logs launches instead of running them). |
| `hey-claude setup` / `doctor` / `calibrate [--reuse]` | Models, health check, wake-word tuning. |
| `hey-claude mute` / `unmute` / `toggle-mute` / `cancel` | Control a running instance (mute turns off the wake word; push-to-talk still works). |
| `hey-claude ptt start\|stop\|toggle` | Push-to-talk, for key bindings. |
| `hey-claude status [--json\|--waybar] [--watch]` | Current state; `--watch` streams changes (see [contrib/waybar.jsonc](contrib/waybar.jsonc)). |
| `hey-claude launch [--dry-run] [-p PROJECT] [PROMPT…]` | Test the launcher without speaking. |
| `hey-claude simulate FILE.wav [--ptt 3.0:5.5]` | Run the whole pipeline over a recording; prints what would launch. |
| `hey-claude history` / `devices` | Recent launches; audio devices. |

## Troubleshooting

- **Doesn't hear the wake word:** run `hey-claude calibrate`, check the input device with
  `hey-claude devices` (set `[audio] device`), or add phrase variants.
- **Fires on its own:** calibrate again while reading the paragraph at a normal volume, or raise
  `[wake] threshold` a little (e.g. 0.35).
- **Prompt cut off or merged with the next sentence:** adjust `[wake] end_silence` (seconds of
  pause that end an utterance).
- **Debug a specific case:** record it (`arecord -f S16_LE -r 16000 -c 1 test.wav`), then run
  `hey-claude simulate test.wav -v`.
- **Terminal doesn't open from the systemd unit:** the user manager is missing
  `WAYLAND_DISPLAY`/`DISPLAY`; see the unit file header.

Resource use: about 1 GB RAM with the models loaded. On CPU, Parakeet transcribes a 4 s utterance
in about 0.5 s; the keyword spotter costs a few percent of one core.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev,evdev]'
.venv/bin/pytest
```

The pipeline (`hey_claude/pipeline.py`) is a frame-driven state machine that keeps time by
counting samples, so the tests drive it with scripted fake engines and need neither models nor
a microphone. `simulate` runs the same code over a WAV file with the real models.
