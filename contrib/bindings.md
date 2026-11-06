# Push-to-talk and window bindings

Two ways to do push-to-talk:

1. **evdev** (default when available): hold Right Alt anywhere, X11 or Wayland.
   Needs read access to `/dev/input` (`gpasswd -a $USER input`, then log in again).
   That group can read *every* keyboard; hey-claude only acts on the configured
   keys, but if you'd rather not grant it, set `[ptt] backend = "none"` and use (2).
2. **Compositor bindings** calling the control socket, below.

`ptt start` / `ptt stop` give true hold-to-talk where the compositor supports
release bindings; otherwise bind `ptt toggle` (press once to talk, again to send).

## sway / i3

```
# ~/.config/sway/config
bindsym --no-repeat Mod4+grave exec hey-claude ptt start
bindsym --release   Mod4+grave exec hey-claude ptt stop
bindsym Mod4+Shift+grave exec hey-claude toggle-mute

# Float Claude Code windows opened by hey-claude, and let systemd units see Wayland.
for_window [app_id="hey-claude"] floating enable, resize set 1100 750, move position center
exec systemctl --user import-environment WAYLAND_DISPLAY DISPLAY XDG_CURRENT_DESKTOP
exec hey-claude run          # if not using the systemd unit
```

i3 uses the same `bindsym`/`--release` lines and `for_window [class="hey-claude"]`.

With `[launch] mode = "tmux"`, bring the existing window forward after a prompt:

```toml
[launch]
focus_command = ["swaymsg", "[app_id=hey-claude] focus"]
```

## Hyprland

```
# ~/.config/hypr/hyprland.conf
bind  = SUPER, grave, exec, hey-claude ptt start
bindr = SUPER, grave, exec, hey-claude ptt stop
bind  = SUPER SHIFT, grave, exec, hey-claude toggle-mute
windowrulev2 = float, class:^(hey-claude)$
windowrulev2 = size 1100 750, class:^(hey-claude)$
exec-once = hey-claude run
```

`focus_command = ["hyprctl", "dispatch", "focuswindow", "class:hey-claude"]`

## KDE Plasma / GNOME / XFCE

Add a custom shortcut running `hey-claude ptt toggle` (these desktops don't
offer key-release shortcuts). Window class/app-id is `hey-claude` for kitty,
foot, alacritty, wezterm, urxvt, st and xterm.
