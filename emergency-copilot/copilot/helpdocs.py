"""Keyword help search over data/help.json. Used by the support agent's search_help tool, as the no-AI fallback,
and embedded (same data, same scoring) in the static pages."""
from __future__ import annotations

import json
import re
from pathlib import Path

_STOP = {"the", "a", "an", "is", "are", "how", "do", "does", "i", "to", "of", "and", "what", "it", "in", "my", "can", "for", "on", "be", "or"}


def _tok(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9']+", s.lower()) if t not in _STOP and len(t) > 1}


class HelpIndex:
    def __init__(self, path: Path | None = None):
        path = path or Path(__file__).parent / "data" / "help.json"
        self.entries = json.loads(path.read_text(encoding="utf-8"))["entries"]

    def search(self, query: str, n: int = 3) -> list[dict]:
        qt = _tok(query)
        scored = []
        for e in self.entries:
            kw = {w for k in e["keywords"] for w in _tok(k)}
            score = 3 * len(qt & kw) + 2 * len(qt & _tok(e["q"])) + len(qt & _tok(e["a"]))
            if score:
                scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        return [{"q": e["q"], "a": e["a"]} for _, e in scored[:n]]
