"""Turn a raw transcript into a launch request (pure text logic, no audio)."""

from __future__ import annotations

import re
from dataclasses import dataclass

# How the ASR model tends to spell the wake phrase. The transcript covers the whole
# utterance including the wake word (we keep pre-roll audio), so it has to be removed.
_GREETING = r"(?:hey|hay|hei|hi|a|ok|okay|yo)"
_NAME = r"(?:claude|claud|clawed|clod|cloud|clyde|klaud|klod|claudia)"
_WAKE_RE = re.compile(rf"^\W*(?:{_GREETING}[\s,.!-]+)?{_NAME}\b[\s,.!?:;-]*", re.I)
# Misheard greetings ("Pay Claude", "Okay so Claude"): any one or two short words
# before the name. Only used for wake-word utterances, where the start *is* the wake phrase.
_LOOSE_WAKE_RE = re.compile(rf"^\W*(?:[a-z']{{1,8}}[\s,.!-]+){{1,2}}?{_NAME}\b[\s,.!?:;-]*", re.I)

# "in <project>, ...", "open <project> and ...", "for project <name>: ..."
_PROJECT_RE = re.compile(
    r"^(?:in|on|for|open|inside|within)\s+(?:the\s+)?(?:project\s+)?(?P<rest>.*)$", re.I | re.S
)
_JOINER_RE = re.compile(r"^(?:project\b)?[\s,.:;-]*(?:(?:and|then|to)\b)?[\s,.:;-]*", re.I)


@dataclass(frozen=True)
class Request:
    prompt: str
    # Project key from config.projects, or None for the default workdir.
    project: str | None = None


def normalize(text: str) -> str:
    """Lowercase, and treat punctuation (hyphens, dots, commas...) as spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[\W_]", " ", text.lower())).strip()


def strip_wake(text: str, phrases: list[str] | None = None, *, loose: bool = False) -> tuple[str, bool]:
    """Remove a leading wake phrase. Returns (rest, whether one was found).

    `loose` also accepts a misheard greeting; use it only when the wake word was
    acoustically detected at the start of this utterance.
    """
    text = text.strip()
    for phrase in phrases or []:
        # Configured phrases first, matched loosely on word boundaries.
        words = [re.escape(w) for w in phrase.split()]
        if not words:
            continue
        pat = re.compile(r"^\W*" + r"[\s,.!-]+".join(words) + r"\b[\s,.!?:;-]*", re.I)
        m = pat.match(text)
        if m:
            return text[m.end():].strip(), True
    m = _WAKE_RE.match(text) or (_LOOSE_WAKE_RE.match(text) if loose else None)
    if m:
        return text[m.end():].strip(), True
    return text, False


def clean_prompt(text: str) -> str:
    text = text.strip()
    # A spoken prompt that is only punctuation or a lone filler word is no prompt.
    if re.fullmatch(r"[\W_]*(?:(?:um+|uh+|er+|hmm+|yeah|okay|ok|so)[\W_]*)?", text, re.I):
        return ""
    # Never let a transcript be parsed as a CLI flag by `claude`.
    return text.lstrip("-").strip()


def match_project(text: str, projects: dict[str, str]) -> tuple[str | None, str]:
    """Route "in <project>, do X" to a configured project. Longest name wins."""
    m = _PROJECT_RE.match(text.strip())
    if not m or not projects:
        return None, text
    rest = m.group("rest")
    norm_rest = normalize(rest)
    for name in sorted(projects, key=len, reverse=True):
        norm_name = normalize(name)
        if norm_rest == norm_name or norm_rest.startswith(norm_name + " "):
            # Walk the original string past as many words as the name has.
            n_words = len(norm_name.split())
            tokens = re.split(r"([\W_]+)", rest)
            consumed, words = 0, 0
            for tok in tokens:
                consumed += len(tok)
                if tok and not re.fullmatch(r"[\W_]+", tok):
                    words += 1
                    if words == n_words:
                        break
            remainder = rest[consumed:]
            remainder = _JOINER_RE.sub("", remainder, count=1)
            return name, remainder.strip()
    return None, text


def parse(text: str, *, phrases: list[str] | None = None,
          projects: dict[str, str] | None = None) -> Request:
    rest, _found = strip_wake(text, phrases)
    project, rest = match_project(rest, projects or {})
    return Request(prompt=clean_prompt(rest), project=project)
