"""Global push-to-talk via evdev (works on X11 and Wayland alike).

Reading /dev/input requires membership in the `input` group. Only the configured
key codes are acted on; nothing is logged or stored. Users who prefer not to grant
that access can bind `hey-claude ptt start/stop` in their compositor instead.
"""

from __future__ import annotations

import logging
import os
import selectors
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


def available() -> tuple[bool, str]:
    try:
        import evdev  # noqa: F401
    except ImportError:
        return False, "python evdev module not installed (pip install 'hey-claude[evdev]')"
    nodes = [f for f in os.listdir("/dev/input") if f.startswith("event")] if os.path.isdir("/dev/input") else []
    if not nodes:
        return False, "no /dev/input/event* devices"
    if not any(os.access(f"/dev/input/{n}", os.R_OK) for n in nodes):
        return False, "no read access to /dev/input (add yourself to the `input` group)"
    return True, ""


class HotkeyListener(threading.Thread):
    RESCAN_SECONDS = 10

    def __init__(self, key: str, cancel_key: str, *, on_press: Callable[[], None],
                 on_release: Callable[[], None], on_cancel: Callable[[], None]) -> None:
        super().__init__(name="hotkey", daemon=True)
        from evdev import ecodes

        self.ecodes = ecodes
        try:
            self.key = ecodes.ecodes[key]
            self.cancel_key = ecodes.ecodes[cancel_key] if cancel_key else None
        except KeyError as e:
            raise ValueError(f"unknown key name {e.args[0]!r} (use evdev names such as KEY_RIGHTALT)") from None
        self.on_press, self.on_release, self.on_cancel = on_press, on_release, on_cancel
        self._stop = threading.Event()
        self._held = False

    def _keyboards(self) -> dict:
        import evdev

        found = {}
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            keys = dev.capabilities().get(self.ecodes.EV_KEY, [])
            if self.key in keys:
                found[path] = dev
            else:
                dev.close()
        return found

    def run(self) -> None:
        sel = selectors.DefaultSelector()
        devices: dict = {}
        last_scan = 0.0
        while not self._stop.is_set():
            if time.monotonic() - last_scan > self.RESCAN_SECONDS:
                last_scan = time.monotonic()
                for path, dev in self._keyboards().items():
                    if path in devices:
                        dev.close()
                        continue
                    devices[path] = dev
                    sel.register(dev, selectors.EVENT_READ)
                    log.info("push-to-talk listening on %s (%s)", dev.name, path)
            if not devices:
                self._stop.wait(1)
                continue
            for key, _ in sel.select(timeout=1):
                dev = key.fileobj
                try:
                    events = list(dev.read())
                except OSError:  # unplugged
                    sel.unregister(dev)
                    devices.pop(dev.path, None)
                    continue
                for ev in events:
                    self._handle(ev)

    def _handle(self, ev) -> None:
        if ev.type != self.ecodes.EV_KEY:
            return
        if ev.code == self.key:
            if ev.value == 1 and not self._held:
                self._held = True
                self.on_press()
            elif ev.value == 0 and self._held:
                self._held = False
                self.on_release()
        elif ev.code == self.cancel_key and ev.value == 1:
            self.on_cancel()

    def stop(self) -> None:
        self._stop.set()
