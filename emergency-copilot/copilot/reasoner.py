"""Severity scoring and summary (feature 10, 25).

RuleReasoner is the deterministic baseline: every point of the score is tied to a cue
and its evidence, so the call-taker sees *why*. An LLM-backed reasoner can implement the
same `Reasoner` protocol and run on stable segments (1.5-3 s budget) in parallel; the
orchestrator never lets it overwrite call-taker decisions.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .extractor import Extraction
from .models import LocCandidate, Severity, SeverityReason

TAXONOMY = json.loads((Path(__file__).parent / "data" / "taxonomy.json").read_text(encoding="utf-8"))
_CUE_POINTS = {
    "not_breathing": 40, "unconscious": 30, "breathing_difficulty": 25, "heavy_bleeding": 20, "weapon": 25,
    "stabbing_shooting": 35, "child": 10, "fire_spreading": 20, "trapped": 20, "hazard_gas": 15,
}
_LEVELS = [(80, "P1", "Critical"), (60, "P2", "High"), (35, "P3", "Medium"), (0, "P4", "Low")]


class Reasoner(Protocol):
    def severity(self, ex: Extraction) -> Severity: ...
    def summary(self, ex: Extraction, best_loc: LocCandidate | None, detail: list[str]) -> str: ...


class RuleReasoner:
    def severity(self, ex: Extraction) -> Severity:
        base = TAXONOMY[ex.incident_type]["base"]
        reasons = [SeverityReason(label=f"Incident type: {TAXONOMY[ex.incident_type]['label']}", points=base,
                                  evidence=", ".join(ex.type_evidence))]
        score = base
        for c in ex.cues:
            pts = _CUE_POINTS.get(c.id, 10)
            reasons.append(SeverityReason(label=c.label, points=pts, evidence=c.evidence))
            score += pts
        if ex.persons and ex.persons >= 3:
            reasons.append(SeverityReason(label=f"{ex.persons} people involved", points=10, evidence=ex.persons_evidence))
            score += 10
        score = min(100, score)
        for floor, code, name in _LEVELS:
            if score >= floor:
                return Severity(score=score, level=code, name=name, reasons=reasons)
        raise AssertionError

    def summary(self, ex: Extraction, best_loc: LocCandidate | None, detail: list[str]) -> str:
        t = TAXONOMY[ex.incident_type]
        parts = [t["label"]]
        if best_loc:
            loc = best_loc.name + (f" ({', '.join(detail)})" if detail else "")
            parts.append(f"at {loc}" + ("" if best_loc.confidence >= 0.6 else " (unconfirmed)"))
        else:
            parts.append("location not yet confirmed")
        facts = []
        if ex.persons:
            facts.append(f"{ex.persons} people")
        if ex.ages:
            facts.append("age " + "/".join(str(a) for a in ex.ages))
        if ex.injuries:
            facts.append("injuries: " + ", ".join(ex.injuries))
        if ex.weapons:
            facts.append("weapon mentioned")
        if ex.vehicles:
            facts.append("vehicles: " + ", ".join(ex.vehicles))
        if ex.hazards:
            facts.append("hazards: " + ", ".join(ex.hazards))
        s = " ".join(parts) + "."
        if facts:
            s += " " + "; ".join(facts).capitalize() + "."
        if ex.cues:
            s += " ALERTS: " + ", ".join(c.label for c in ex.cues) + "."
        return s
