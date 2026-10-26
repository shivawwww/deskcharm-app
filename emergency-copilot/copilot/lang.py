"""Language, Arabic-dialect and code-switch detection (feature 2 / 3, heuristic baseline).

A production system replaces this with a neural LID model running every 1-2 s; the
interface (detect_lang -> LangInfo) stays the same.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .text import norm

_DIALECT_MARKERS = {
    "gulf": ["شلون", "شنو", "وين", "ابي", "ابغي", "ابغى", "الحين", "هالحين", "يمديك", "زين", "مب", "عيال", "جذي", "شفيك", "ويش"],
    "levantine": ["شو", "هلق", "كتير", "بدي", "عم", "هيك", "هون", "منيح", "لسا", "ليش"],
    "egyptian": ["ايه", "دلوقتي", "عايز", "عايزه", "ازاي", "كده", "ده", "دي", "بتاع", "اهو", "حصل", "الحقني"],
    "maghrebi": ["واش", "بزاف", "دابا", "كيفاش", "ديال", "مزيان", "غادي", "راه"],
}
_MARKERS = {d: {norm(w) for w in ws} for d, ws in _DIALECT_MARKERS.items()}
_ARABIC_TOK = re.compile(r"[؀-ۿ]+")
_LATIN_TOK = re.compile(r"[A-Za-z]{2,}")


@dataclass
class LangInfo:
    lang: str            # "ar" | "en" | "mixed" | "unknown"
    dialect: str | None  # gulf | levantine | egyptian | maghrebi | msa | None
    code_switched: bool


def detect_lang(text: str) -> LangInfo:
    ar = _ARABIC_TOK.findall(text)
    la = _LATIN_TOK.findall(text)
    if not ar and not la:
        return LangInfo("unknown", None, False)
    if not ar:
        return LangInfo("en", None, False)
    toks = norm(text).split()
    scores = {d: sum(1 for t in toks if t in m) for d, m in _MARKERS.items()}
    best = max(scores, key=scores.get)
    dialect = best if scores[best] > 0 else "msa"
    if la:
        return LangInfo("mixed", dialect, True)
    return LangInfo("ar", dialect, False)
