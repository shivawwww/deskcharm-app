"""Text normalisation shared by every NLP stage.

normalize() keeps a 1:1 index map back to the original string so evidence
spans can be highlighted in the transcript exactly as spoken/typed.
"""
from __future__ import annotations

import re

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_DROP = {chr(c) for c in range(0x064B, 0x0660)} | {"ٰ", "ـ"}  # tashkeel, tatweel
_MAP = {"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي", "’": "'"}
_AR = re.compile(r"[؀-ۿ]")

# optional Arabic clitics / suffixes around a keyword (wa-, bi-, li-, al-, -ha, -hum, -oo ...)
_AR_PRE = r"(?:[وف])?(?:بي|[بلك])?(?:ال)?"
_AR_SUF = r"(?:ه|ها|هم|ني|ي|ك|ات|ين|ون|وا)?"


def normalize(text: str) -> tuple[str, list[int]]:
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if ch in _DROP:
            continue
        ch = _MAP.get(ch, ch).translate(_DIGITS)
        low = ch.lower()
        ch = low if len(low) == 1 else ch
        if not (ch.isalnum() or ch == "'"):
            ch = " "
        out.append(ch)
        idx.append(i)
    return "".join(out), idx


def norm(text: str) -> str:
    return normalize(text)[0]


def has_arabic(text: str) -> bool:
    return bool(_AR.search(text))


def original_span(original: str, idx: list[int], start: int, end: int) -> str:
    if not idx or start >= end:
        return ""
    return original[idx[start]: idx[min(end, len(idx)) - 1] + 1]


def term_regex(term: str) -> str:
    """Arabic entries are plain words (clitics handled); Latin entries are regex fragments."""
    if has_arabic(term):
        words = norm(term).split()
        body = r"\s+".join(re.escape(w) for w in words)
        return rf"(?<!\w){_AR_PRE}{body}{_AR_SUF}(?!\w)"
    return rf"(?<![\w'])(?:{term})(?![\w'])"


def compile_terms(terms: list[str]) -> re.Pattern:
    return re.compile("|".join(f"(?:{term_regex(t)})" for t in terms))


NEGATORS = {"no", "not", "without", "never", "none", "nobody", "neither", "بدون", "ما", "لا", "مافي", "مو", "مب", "مش", "محد"}


def negated(normalized: str, start: int, window: int = 3) -> bool:
    prev = normalized[:start].split()[-window:]
    return any(t in NEGATORS for t in prev)
