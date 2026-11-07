from types import SimpleNamespace

import pytest

ecodes = pytest.importorskip("evdev").ecodes

from hey_claude.hotkey import HotkeyListener  # noqa: E402


def key_event(code, value):
    return SimpleNamespace(type=ecodes.EV_KEY, code=code, value=value)


def test_holding_a_chord_key_is_one_press_and_one_release():
    # A Copilot key held for a moment: Meta + Shift + F23, each repeating until let go.
    calls = []
    listener = HotkeyListener("KEY_F23", "KEY_ESC", on_press=lambda: calls.append("press"),
                              on_release=lambda: calls.append("release"),
                              on_cancel=lambda: calls.append("cancel"))
    meta, shift, f23 = ecodes.KEY_LEFTMETA, ecodes.KEY_LEFTSHIFT, ecodes.KEY_F23
    events = [(meta, 1), (shift, 1), (f23, 1),
              (meta, 2), (shift, 2), (f23, 2), (meta, 2), (shift, 2), (f23, 2),
              (f23, 0), (shift, 0), (meta, 0)]
    for code, value in events:
        listener._handle(key_event(code, value))
    assert calls == ["press", "release"]
