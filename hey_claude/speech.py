"""Thin wrappers around sherpa-onnx: keyword spotting, voice activity and transcription.

Everything runs locally on the CPU; audio never leaves the machine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from hey_claude import models

SAMPLE_RATE = 16000
# Silero VAD consumes 512-sample windows at 16 kHz; the mic delivers frames of this size.
FRAME = 512


def _sherpa():
    import sherpa_onnx  # imported lazily so pure-logic modules and tests don't need it

    return sherpa_onnx


def phrase_tokens(phrase: str, bpe_model: Path) -> list[str]:
    import sentencepiece as spm

    sp = spm.SentencePieceProcessor(model_file=str(bpe_model))
    # The GigaSpeech KWS vocabulary is upper-case BPE.
    return sp.encode(phrase.upper().strip(), out_type=str)


def label(phrase: str) -> str:
    return re.sub(r"\W+", "_", phrase.strip().upper()).strip("_")


def keywords_text(phrases: list[str], kws_dir: Path, threshold: float, boost: float) -> str:
    """Build sherpa-onnx keyword lines: `tok tok ... :boost #threshold @LABEL`."""
    vocab = {line.split()[0] for line in (kws_dir / "tokens.txt").read_text().splitlines() if line.strip()}
    lines = []
    for phrase in phrases:
        toks = phrase_tokens(phrase, kws_dir / "bpe.model")
        unknown = [t for t in toks if t not in vocab]
        if not toks or unknown:
            raise ValueError(f"wake phrase {phrase!r} cannot be encoded (unknown tokens {unknown})")
        lines.append(f"{' '.join(toks)} :{boost:.2f} #{threshold:.2f} @{label(phrase)}")
    return "\n".join(lines)


class WakeWordSpotter:
    def __init__(self, phrases: list[str], threshold: float, boost: float, *,
                 threads: int = 1, provider: str = "cpu", root: Path | None = None,
                 keywords_file: Path | None = None) -> None:
        so = _sherpa()
        self.dir = models.path(models.KWS, root)
        # Per-keyword ":boost #threshold" must go in the keywords *file*: sherpa-onnx
        # ignores those values for keywords passed to create_stream().
        text = keywords_text(phrases, self.dir, threshold, boost)
        # The constructor insists on a keywords file; write ours next to the model.
        kw_file = keywords_file or (self.dir / "hey-claude-keywords.txt")
        kw_file.parent.mkdir(parents=True, exist_ok=True)
        kw_file.write_text(text + "\n")
        stem = "epoch-12-avg-2-chunk-16-left-64.int8.onnx"
        self._kws = so.KeywordSpotter(
            tokens=str(self.dir / "tokens.txt"),
            encoder=str(self.dir / f"encoder-{stem}"),
            decoder=str(self.dir / f"decoder-{stem}"),
            joiner=str(self.dir / f"joiner-{stem}"),
            keywords_file=str(kw_file),
            num_threads=threads,
            provider=provider,
            keywords_threshold=threshold,
            keywords_score=boost,
        )
        self._stream = self._kws.create_stream()

    def accept(self, samples: np.ndarray) -> str | None:
        """Feed audio; return the detected keyword label, if any."""
        s = self._stream
        s.accept_waveform(SAMPLE_RATE, samples)
        hit = None
        while self._kws.is_ready(s):
            self._kws.decode_stream(s)
            result = self._kws.get_result(s)
            if result:
                hit = result
                self._kws.reset_stream(s)
        return hit

    def reset(self) -> None:
        self._stream = self._kws.create_stream()


class VoiceActivity:
    def __init__(self, *, min_silence: float, max_speech: float, root: Path | None = None) -> None:
        so = _sherpa()
        cfg = so.VadModelConfig()
        cfg.silero_vad.model = str(models.path(models.VAD, root))
        cfg.silero_vad.min_silence_duration = min_silence
        cfg.silero_vad.max_speech_duration = max_speech
        cfg.sample_rate = SAMPLE_RATE
        self._vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=max_speech + 5)

    def accept(self, samples: np.ndarray) -> bool:
        """Feed one frame; return whether the detector is currently inside speech."""
        self._vad.accept_waveform(samples)
        # We keep our own utterance buffer, so discard the detector's segments.
        while not self._vad.empty():
            self._vad.pop()
        return bool(self._vad.is_speech_detected())

    def reset(self) -> None:
        self._vad.reset()


@dataclass
class Transcript:
    text: str
    tokens: list[str] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)

    def text_after(self, seconds: float) -> str:
        """Re-assemble the text from the first whole word starting at or after `seconds`.

        Tokens are BPE pieces; a piece beginning with a space starts a new word.
        """
        for i, (tok, ts) in enumerate(zip(self.tokens, self.timestamps)):
            if ts >= seconds and (tok.startswith(" ") or i == 0):
                return "".join(self.tokens[i:]).strip()
        return ""


class Transcriber:
    def __init__(self, *, threads: int = 4, provider: str = "cpu", root: Path | None = None) -> None:
        so = _sherpa()
        d = models.path(models.ASR, root)
        self._rec = so.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.int8.onnx"),
            decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            model_type="nemo_transducer",
            num_threads=threads,
            provider=provider,
        )

    def transcribe(self, samples: np.ndarray) -> Transcript:
        if len(samples) == 0:
            return Transcript("")
        stream = self._rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self._rec.decode_stream(stream)
        r = stream.result
        return Transcript(r.text.strip(), list(r.tokens), [float(t) for t in r.timestamps])
