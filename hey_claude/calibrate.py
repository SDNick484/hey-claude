"""Tune the wake-word threshold to your voice, accent and microphone.

Records a few "Hey Claude" samples plus some ordinary speech, then sweeps the
keyword-spotter's threshold/boost and keeps the strictest setting that still
hears you reliably without firing on the ordinary speech.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hey_claude import audio, config, speech
from hey_claude.config import Config
from hey_claude.speech import SAMPLE_RATE

THRESHOLDS = [0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5]
BOOSTS = [1.0, 1.5, 2.5]

READING = (
    "The quick brown fox jumps over the lazy dog. I opened the cloud console and "
    "checked the logs. Hey, could you send me that file later? My colleague Claudia "
    "is working on the release notes. Let's grab lunch at noon and talk about it."
)


@dataclass
class Score:
    threshold: float
    boost: float
    hits: int
    false_alarms: int


def count(spotter: speech.WakeWordSpotter, clip: np.ndarray) -> int:
    spotter.reset()
    pad = np.zeros(SAMPLE_RATE // 2, np.float32)
    detections = 0
    for frame in audio.frames_of(np.concatenate([pad, clip, pad, pad])):
        if spotter.accept(frame):
            detections += 1
    return detections


def sweep(phrases: list[str], positives: list[np.ndarray], negative: np.ndarray,
          progress=lambda done, total: None) -> list[Score]:
    import tempfile

    scores = []
    total = len(BOOSTS) * len(THRESHOLDS)
    with tempfile.TemporaryDirectory() as tmp:
        for boost in BOOSTS:
            for th in THRESHOLDS:
                # Thresholds only take effect from the keywords file, so build a spotter per setting.
                spotter = speech.WakeWordSpotter(phrases, th, boost,
                                                 keywords_file=Path(tmp) / "keywords.txt")
                hits = sum(1 for clip in positives if count(spotter, clip) > 0)
                scores.append(Score(th, boost, hits, count(spotter, negative)))
                progress(len(scores), total)
    return scores


def choose(scores: list[Score], n_positive: int) -> Score | None:
    """Pick a setting that hears (nearly) every sample with no false alarms, with margin.

    The edge of the passing range overfits a handful of recordings, so take the boost
    with the widest range of passing thresholds and use the middle of that range.
    """
    need = n_positive if n_positive < 4 else n_positive - 1
    good = [s for s in scores if s.hits >= need and s.false_alarms == 0]
    if not good:
        return None
    best_hits = max(s.hits for s in good)
    by_boost: dict[float, list[Score]] = {}
    for s in good:
        if s.hits == best_hits:
            by_boost.setdefault(s.boost, []).append(s)
    boost = max(by_boost, key=lambda b: (len(by_boost[b]), -b))
    passing = sorted(by_boost[boost], key=lambda s: s.threshold)
    return passing[len(passing) // 2]


def _countdown(msg: str) -> None:
    print(msg, flush=True)
    for i in (3, 2, 1):
        print(f"  {i}…", end="\r", flush=True)
        time.sleep(0.7)


def run(cfg: Config, *, samples: int = 5, reuse: bool = False, write: bool = True,
        config_path: Path | None = None) -> int:
    from hey_claude.app import ensure_models
    from hey_claude.feedback import Chimes

    ensure_models()
    out_dir = config.state_dir() / "calibration"
    phrase = cfg.wake.phrases[0]

    if reuse:
        positives = [audio.read_wav(p) for p in sorted(out_dir.glob("wake-*.wav"))]
        neg_path = out_dir / "speech.wav"
        if not positives or not neg_path.exists():
            print(f"no saved recordings in {out_dir}; run without --reuse first")
            return 1
        negative = audio.read_wav(neg_path)
    else:
        chimes = Chimes(True, cfg.audio.chime_volume)
        mic = audio.Microphone(cfg.audio.device)
        mic.start()
        positives = []
        try:
            print(f"Say “{phrase.title()}” naturally after each chime "
                  f"({samples} times, as you normally would).\n")
            for i in range(samples):
                time.sleep(0.8)
                mic.drain()
                chimes.play("listen")
                print(f"  [{i + 1}/{samples}] speak now…", flush=True)
                positives.append(mic.record(2.5))
            _countdown("\nNow read this aloud at a normal pace (it should NOT trigger):\n\n"
                       f"  {READING}\n")
            mic.drain()
            chimes.play("listen")
            print("  recording 15 s…", flush=True)
            negative = mic.record(15)
            chimes.play("launch")
        finally:
            mic.stop()
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, clip in enumerate(positives):
            audio.write_wav(out_dir / f"wake-{i}.wav", clip)
        audio.write_wav(out_dir / "speech.wav", negative)

    loudness = max(float(np.sqrt(np.mean(c ** 2))) for c in positives)
    if loudness < 0.003:
        print("The recordings are nearly silent - check the input device (`hey-claude devices`).")
        return 1

    print("\nEvaluating…")
    scores = sweep(cfg.wake.phrases, positives, negative,
                   progress=lambda d, t: print(f"  {d}/{t}", end="\r", flush=True))
    print(" " * 20, end="\r")
    best = choose(scores, len(positives))
    current = next((s for s in scores if abs(s.threshold - cfg.wake.threshold) < 1e-6
                    and abs(s.boost - cfg.wake.boost) < 1e-6), None)
    if current:
        print(f"  current  threshold={current.threshold:.2f} boost={current.boost:.1f}: "
              f"heard {current.hits}/{len(positives)}, false alarms {current.false_alarms}")
    if not best:
        top = max(scores, key=lambda s: (s.hits - 2 * s.false_alarms, s.threshold))
        print(f"  No setting was both reliable and quiet. Closest: threshold={top.threshold:.2f} "
              f"boost={top.boost:.1f} (heard {top.hits}/{len(positives)}, "
              f"false alarms {top.false_alarms}).")
        print("  Try a closer microphone, or add a variant such as \"hi claude\" to [wake] phrases.")
        return 1
    print(f"  chosen   threshold={best.threshold:.2f} boost={best.boost:.1f}: "
          f"heard {best.hits}/{len(positives)}, false alarms {best.false_alarms}")
    if write:
        path = config.set_wake_tuning(best.threshold, best.boost, config_path)
        print(f"\nSaved to {path}. Restart hey-claude to apply.")
    return 0
