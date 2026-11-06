"""Local control socket (JSON lines over a Unix socket in $XDG_RUNTIME_DIR).

Lets compositor key bindings, bars and scripts drive the daemon:
    {"cmd": "ptt-start"}  -> {"ok": true, "status": {...}}
    {"cmd": "subscribe"}  -> a status line now and on every change
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import threading
from pathlib import Path
from typing import Callable, Iterator

from hey_claude import config

log = logging.getLogger(__name__)

COMMANDS = ("status", "mute", "unmute", "toggle-mute", "ptt-start", "ptt-stop", "ptt-toggle",
            "cancel", "subscribe")


class Hub:
    """Shared between the daemon loop and socket threads: status snapshot + subscribers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: dict = {"state": "starting", "muted": False}
        self._subs: list[Callable[[dict], None]] = []

    @property
    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def publish(self, status: dict) -> None:
        with self._lock:
            self._status = dict(status)
            subs = list(self._subs)
        for fn in subs:
            fn(status)

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return unsubscribe


class Server:
    def __init__(self, hub: Hub, dispatch: Callable[[str], None], path: Path | None = None) -> None:
        self.hub, self.dispatch = hub, dispatch
        self.path = path or config.socket_path()
        self._server: socketserver.ThreadingUnixStreamServer | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            if _alive(self.path):
                raise RuntimeError(f"another hey-claude is already running ({self.path})")
            self.path.unlink()
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                for raw in self.rfile:
                    try:
                        cmd = json.loads(raw).get("cmd")
                    except (ValueError, AttributeError):
                        cmd = None
                    if cmd not in COMMANDS:
                        self._send({"ok": False, "error": f"unknown command {cmd!r}"})
                        continue
                    if cmd == "subscribe":
                        self._stream()
                        return
                    if cmd != "status":
                        outer.dispatch(cmd)
                    self._send({"ok": True, "status": outer.hub.status})

            def _send(self, obj: dict) -> bool:
                try:
                    self.wfile.write((json.dumps(obj) + "\n").encode())
                    self.wfile.flush()
                    return True
                except OSError:
                    return False

            def _stream(self) -> None:
                done = threading.Event()

                def push(status: dict) -> None:
                    if not self._send(status):
                        done.set()

                unsubscribe = outer.hub.subscribe(push)
                try:
                    if self._send(outer.hub.status):
                        # Wake periodically to notice a client that disconnected quietly.
                        while not done.wait(30):
                            if not self._send(outer.hub.status):
                                break
                finally:
                    unsubscribe()

        try:
            self._server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
        except OSError as e:
            raise RuntimeError(f"cannot create control socket {self.path}: {e}") from None
        self._server.daemon_threads = True
        os.chmod(self.path, 0o600)
        threading.Thread(target=self._server.serve_forever, name="control", daemon=True).start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _alive(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(path))
        return True
    except OSError:
        return False


def request(cmd: str, path: Path | None = None, timeout: float = 3.0) -> dict:
    """Send one command to the running daemon and return its reply."""
    path = path or config.socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall((json.dumps({"cmd": cmd}) + "\n").encode())
        with s.makefile("r") as f:
            line = f.readline()
    if not line:
        raise ConnectionError("daemon closed the connection")
    return json.loads(line)


def watch(path: Path | None = None) -> Iterator[dict]:
    """Yield status updates from the running daemon until it goes away."""
    path = path or config.socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(path))
        s.sendall(b'{"cmd": "subscribe"}\n')
        with s.makefile("r") as f:
            for line in f:
                yield json.loads(line)
