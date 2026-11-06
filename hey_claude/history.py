"""Recent launches, appended as JSON lines under $XDG_STATE_HOME/hey-claude."""

from __future__ import annotations

import json
import time
from pathlib import Path

from hey_claude import config

KEEP = 200


def path() -> Path:
    return config.state_dir() / "history.jsonl"


def record(entry: dict, file: Path | None = None) -> None:
    file = file or path()
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a") as f:
        f.write(json.dumps({"time": time.time(), **entry}) + "\n")
    lines = file.read_text().splitlines()
    if len(lines) > KEEP * 2:
        file.write_text("\n".join(lines[-KEEP:]) + "\n")


def recent(n: int = 10, file: Path | None = None) -> list[dict]:
    file = file or path()
    if not file.exists():
        return []
    out = []
    for line in file.read_text().splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
