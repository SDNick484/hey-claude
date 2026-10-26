"""Download and locate the on-device speech models (all from sherpa-onnx releases)."""

from __future__ import annotations

import shutil
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from hey_claude import config

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class ModelSpec:
    key: str
    url: str
    # Directory (for archives) or file name (for single files) under models_dir().
    target: str
    # Files that must exist under target for the model to count as installed.
    required: tuple[str, ...]
    # Archive members that are not needed at runtime and get deleted after extraction.
    prune: tuple[str, ...] = ()
    approx_mb: int = 0


KWS = ModelSpec(
    key="kws",
    url=f"{RELEASES}/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2",
    target="sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01",
    required=(
        "tokens.txt",
        "bpe.model",
        "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    ),
    prune=(
        "test_wavs",
        "encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        "joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
    ),
    approx_mb=18,
)

VAD = ModelSpec(
    key="vad",
    url=f"{RELEASES}/asr-models/silero_vad.onnx",
    target="silero_vad.onnx",
    required=(),
    approx_mb=1,
)

ASR = ModelSpec(
    key="asr",
    url=f"{RELEASES}/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2",
    target="sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
    required=("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"),
    prune=("test_wavs",),
    approx_mb=480,
)

ALL = (KWS, VAD, ASR)


def path(spec: ModelSpec, root: Path | None = None) -> Path:
    return (root or config.models_dir()) / spec.target


def installed(spec: ModelSpec, root: Path | None = None) -> bool:
    p = path(spec, root)
    if not spec.required:
        return p.is_file()
    return all((p / f).is_file() for f in spec.required)


def missing(root: Path | None = None) -> list[ModelSpec]:
    return [s for s in ALL if not installed(s, root)]


def _download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url) as resp, dest.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total and sys.stderr.isatty():
                pct = done * 100 // total
                print(f"\r  {done >> 20} / {total >> 20} MiB ({pct}%)", end="", file=sys.stderr)
    if total and sys.stderr.isatty():
        print(file=sys.stderr)


def fetch(spec: ModelSpec, root: Path | None = None) -> Path:
    root = root or config.models_dir()
    root.mkdir(parents=True, exist_ok=True)
    dest = path(spec, root)
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        tmp_path = Path(tmp)
        download = tmp_path / spec.url.rsplit("/", 1)[1]
        _download(spec.url, download)
        if not spec.url.endswith((".tar.bz2", ".tar.gz")):
            download.replace(dest)
            return dest
        with tarfile.open(download) as tar:
            # "data" filter rejects absolute paths, traversal and device files.
            tar.extractall(tmp_path / "x", filter="data")
        extracted = tmp_path / "x" / spec.target
        if not extracted.is_dir():
            raise RuntimeError(f"archive {download.name} did not contain {spec.target}/")
        for name in spec.prune:
            victim = extracted / name
            if victim.is_dir():
                shutil.rmtree(victim)
            elif victim.exists():
                victim.unlink()
        if dest.exists():
            shutil.rmtree(dest)
        extracted.replace(dest)
    return dest
