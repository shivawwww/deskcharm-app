"""Location engine (features 17, 19, 21): spoken address -> ranked, scored candidates.

Hybrid matching against the local gazetteer (n-gram fuzzy match on normalised text plus
a Latin phonetic skeleton for transliteration variants), then fusion with AML handset
position. Output is always a ranked list with confidence, never a single silent guess.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from .models import LocCandidate
from .text import has_arabic, normalize, norm, original_span

DATA = Path(__file__).parent / "data"
MIN_SCORE = 0.80


_AL = re.compile(r"^(?:[وفبلك]?ال|لل)")


def _strip_al(tok: str) -> str:
    m = _AL.match(tok)
    return tok[m.end():] if m and len(tok) - m.end() >= 3 else tok


def _skeleton(phrase: str) -> str:
    """Consonant skeleton so 'Shaikh Zaid' == 'Sheikh Zayed' (Latin only)."""
    s = re.sub(r"[^a-z]", "", phrase.lower())
    for a, b in (("sh", "s"), ("kh", "k"), ("ph", "f"), ("ch", "c"), ("qu", "k"), ("q", "k"), ("z", "s"), ("c", "k"), ("w", "v")):
        s = s.replace(a, b)
    s = re.sub(r"[aeiouyh]", "", s)
    return re.sub(r"(.)\1+", r"\1", s)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class _Alias:
    entry: dict
    tokens: list[str]
    phrase: str
    skel: str
    arabic: bool


class Gazetteer:
    def __init__(self, entries: list[dict]):
        self.entries = entries
        self.aliases: list[_Alias] = []
        for e in entries:
            for a in e["aliases"]:
                n = norm(a)
                toks = [_strip_al(t) for t in n.split()]
                phrase = " ".join(toks)
                ar = has_arabic(a)
                self.aliases.append(_Alias(e, toks, phrase, "" if ar else _skeleton(phrase), ar))
        self.by_len: dict[int, list[_Alias]] = {}
        for al in self.aliases:
            self.by_len.setdefault(len(al.tokens), []).append(al)

    @classmethod
    def load(cls, path: Path | None = None) -> "Gazetteer":
        path = path or DATA / "gazetteer.json"
        return cls(json.loads(path.read_text(encoding="utf-8"))["entries"])

    def hotwords(self) -> list[str]:
        """Custom vocabulary for ASR contextual biasing (feature 6)."""
        words: list[str] = []
        for e in self.entries:
            words += [e["name"], e.get("name_ar", "")] + e["aliases"]
        return sorted({w for w in words if w})


class LocationEngine:
    def __init__(self, gazetteer: Gazetteer | None = None):
        self.gaz = gazetteer or Gazetteer.load()

    def match_text(self, text: str) -> list[LocCandidate]:
        n, idx = normalize(text)
        toks = [(m.group(), m.start(), m.end()) for m in re.finditer(r"\S+", n)]
        best: dict[str, tuple[float, int, int, int]] = {}
        for size, aliases in self.gaz.by_len.items():
            for i in range(0, len(toks) - size + 1):
                gram = toks[i: i + size]
                words = [_strip_al(g[0]) for g in gram]
                phrase = " ".join(words)
                latin = not has_arabic(phrase)
                if len(phrase) < (4 if latin else 3):
                    continue
                gskel = _skeleton(phrase) if latin else ""
                for al in aliases:
                    if al.arabic == latin or abs(len(al.phrase) - len(phrase)) > 4:
                        continue
                    sm = SequenceMatcher(None, phrase, al.phrase)
                    if sm.real_quick_ratio() < MIN_SCORE - 0.1 or sm.quick_ratio() < MIN_SCORE - 0.1:
                        score = 0.0
                    else:
                        score = sm.ratio()
                    if latin and len(gskel) >= 4 and gskel == al.skel:
                        score = max(score, 0.9)
                    score += 0.01 * size  # prefer the longer, more specific alias
                    if score >= MIN_SCORE:
                        cur = best.get(al.entry["id"])
                        if cur is None or score > cur[0]:
                            best[al.entry["id"]] = (score, gram[0][1], gram[-1][2], i)
        out: list[LocCandidate] = []
        by_id = {e["id"]: e for e in self.gaz.entries}
        for eid, (score, s, e, _) in best.items():
            ent = by_id[eid]
            conf = 0.5 + (min(score, 1.0) - MIN_SCORE) / (1 - MIN_SCORE) * 0.45
            out.append(LocCandidate(name=ent["name"], name_ar=ent.get("name_ar"), kind=ent["type"], lat=ent["lat"], lon=ent["lon"],
                                    region=ent.get("region"), area=ent.get("area"),
                                    confidence=round(conf, 2), source="speech", evidence=original_span(text, idx, s, e)))
        return sorted(out, key=lambda c: (c.confidence, len(c.evidence)), reverse=True)

    def resolve(self, text: str, aml: dict | None = None) -> list[LocCandidate]:
        cands = self.match_text(text)
        if not aml:
            return cands[:4]
        acc = float(aml.get("accuracy_m", 500))
        aml_conf = round(max(0.3, 0.97 - min(acc, 1000) / 1500), 2)
        for c in cands:
            d = haversine_m(c.lat, c.lon, aml["lat"], aml["lon"])
            if d <= max(400.0, 3 * acc):
                c.confidence = round(min(0.99, c.confidence + 0.15), 2)
                c.note = f"corroborated by handset location ({int(d)} m away)"
            elif d > 2000:
                c.confidence = round(c.confidence * 0.7, 2)
                c.note = f"conflicts with handset location ({d / 1000:.1f} km away), verify"
        cands.append(LocCandidate(name="Handset location (AML)", kind="aml", lat=aml["lat"], lon=aml["lon"],
                                  confidence=aml_conf, source="aml", note=f"accuracy ±{int(acc)} m"))
        return sorted(cands, key=lambda c: (c.confidence, len(c.evidence)), reverse=True)[:4]
