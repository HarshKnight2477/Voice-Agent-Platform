"""Deterministic detection of a caller's yes/no to a read-back.

Confirmation of state-changing actions must not depend on the model's judgement alone, so
the runtime classifies the caller's reply with plain rules. Ambiguous replies are not a yes.
"""

from __future__ import annotations

import re

_NEGATIVE = re.compile(
    r"\b(no|nope|nah|not|don't|dont|wait|hold on|stop|cancel|wrong|change|actually)\b", re.I
)
_AFFIRMATIVE = re.compile(
    r"^\W*(yes|yeah|yep|yup|sure|correct|right|exactly|absolutely|definitely|ok|okay|"
    r"that's right|thats right|that is right|that's correct|sounds good|go ahead|please do|"
    r"do it|book it|confirm|confirmed|perfect|great)\b",
    re.I,
)


def is_affirmative(utterance: str) -> bool:
    text = utterance.strip()
    if not text or len(text) > 120:
        return False
    if _NEGATIVE.search(text):
        return False
    return bool(_AFFIRMATIVE.search(text))
