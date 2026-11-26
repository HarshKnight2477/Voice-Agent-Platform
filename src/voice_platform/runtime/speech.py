"""Output guard: turn model text into something safe and natural to speak.

The prompt asks for short spoken replies, but the guard enforces it: no markdown, no lists,
no raw URLs or markup, and a hard length cap at a sentence boundary.
"""

from __future__ import annotations

import re

MAX_SPOKEN_CHARS = 420

_MARKDOWN = re.compile(r"(\*+|__|`+|#+\s|^\s*[-•]\s+|^\s*\d+\.\s+)", re.M)
_URL = re.compile(r"https?://\S+")
_TAGS = re.compile(r"<[^>]{1,200}>")
_SPACES = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def to_speech(text: str, max_chars: int = MAX_SPOKEN_CHARS) -> str:
    out = _TAGS.sub(" ", text)
    out = _URL.sub("the link on our website", out)
    out = _MARKDOWN.sub(" ", out)
    out = _SPACES.sub(" ", out).strip()
    out = re.sub(r"\s+([.,!?;:])", r"\1", out)
    if len(out) <= max_chars:
        return out
    # Cut at the last sentence end that fits; never mid-word.
    cut = out[:max_chars]
    ends = [m.start() for m in _SENTENCE_END.finditer(cut)]
    if ends:
        return cut[: ends[-1]].strip()
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "."
