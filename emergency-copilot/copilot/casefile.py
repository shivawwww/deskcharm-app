"""Case record: region, area, nearest facilities, agencies and timeline for one call (feeds the CAD incident).

Reference data (regions, facilities, agency hotlines) lives in data/*.json and is DEMO data. Distances are
straight-line, not drive time: unit ETA and recommendation are Phase 2 features and are not claimed here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .location import haversine_m
from .models import CallState
from .reasoner import TAXONOMY

DATA = Path(__file__).parent / "data"
_MEDICAL_CUES = {"not_breathing", "unconscious", "breathing_difficulty", "heavy_bleeding", "stabbing_shooting"}
REGION_MAX_KM = 40.0  # beyond this from any known place the region is flagged as outside gazetteer coverage


def _load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


class Reference:
    def __init__(self, gazetteer_entries: list[dict]):
        self.places = gazetteer_entries
        self.regions = _load("regions.json")["regions"]
        self.facilities = _load("facilities.json")["facilities"]
        self.agencies = _load("agencies.json")["agencies"]
        self._region_by_name = {r["name"]: r for r in self.regions}

    def region(self, name: str | None) -> dict | None:
        return self._region_by_name.get(name or "")

    def locate(self, lat: float, lon: float) -> dict:
        """Region / area for a coordinate: nearest gazetteer place, else nearest region centre."""
        best = min(self.places, key=lambda p: haversine_m(lat, lon, p["lat"], p["lon"]))
        d = haversine_m(lat, lon, best["lat"], best["lon"]) / 1000
        if d <= REGION_MAX_KM:
            return {"region": best["region"], "area": best["area"], "nearest_place": best["name"], "distance_km": round(d, 1)}
        r = min(self.regions, key=lambda r: haversine_m(lat, lon, r["lat"], r["lon"]))
        return {"region": r["name"], "area": None, "nearest_place": None, "distance_km": None, "outside_coverage": True}

    def nearest(self, lat: float, lon: float, kind: str, n: int = 1) -> list[dict]:
        items = [f for f in self.facilities if f["kind"] == kind]
        items.sort(key=lambda f: haversine_m(lat, lon, f["lat"], f["lon"]))
        return [{**f, "distance_km": round(haversine_m(lat, lon, f["lat"], f["lon"]) / 1000, 1)} for f in items[:n]]


def build_case(st: CallState, ref: Reference, include_ai: bool = True) -> dict[str, Any]:
    case: dict[str, Any] = {
        "case_number": st.case_number, "status": "open" if st.status == "active" else "closed",
        "opened_at": st.started_at, "closed_at": st.ended_at,
        "caller": {"number": st.caller_number, "lang": st.lang, "dialect": st.dialect, "code_switched": st.code_switched},
        "timeline": [e for e in st.timeline if include_ai or not e.get("ai")],
    }
    if not include_ai:
        return case

    inc, loc, pri = st.fields.get("incident_type"), st.fields.get("location"), st.fields.get("priority")
    case["incident"] = {"label": inc.value, **inc.meta, "status": inc.status} if inc else None
    case["priority"] = {"value": pri.value, "status": pri.status, "score": st.severity.score} if pri else None
    case["people"] = {k: st.fields[k].value for k in ("persons", "ages", "injuries", "weapons", "vehicles", "hazards") if k in st.fields}
    case["cues"] = [{"id": c.id, "label": c.label, "acked": c.acked} for c in st.cues.values()]
    case["location"] = None
    case["facilities"] = []
    case["agencies"] = []
    best = st.candidates[0] if st.candidates else None
    if best:
        where = ref.locate(best.lat, best.lon)
        region = best.region or where["region"]
        reg = ref.region(region)
        case["location"] = {
            "name": loc.value if loc else best.name, "name_ar": best.name_ar, "lat": best.lat, "lon": best.lon,
            "confidence": loc.confidence if loc else best.confidence, "status": loc.status if loc else "suggested",
            "region": region, "region_ar": reg["name_ar"] if reg else None, "area": best.area or where["area"],
            "nearest_place": where["nearest_place"], "outside_coverage": where.get("outside_coverage", False),
            "detail": st.fields["address_detail"].value if "address_detail" in st.fields else None,
            "source": best.source, "alternatives": [c.name for c in st.candidates[1:]],
        }
        agencies = list(inc.meta.get("agencies", [])) if inc else []
        kinds = {ref.agencies[a]["facility"]: a for a in agencies if a in ref.agencies}
        if set(st.cues) & _MEDICAL_CUES:
            kinds.setdefault("hospital", "Ambulance")
        for kind, agency in kinds.items():
            for f in ref.nearest(best.lat, best.lon, kind, 1):
                case["facilities"].append({**f, "agency": agency})
        case["agencies"] = [{"name": a, "hotline": ref.agencies.get(a, {}).get("hotline")} for a in agencies]
        if "Ambulance" not in agencies and set(st.cues) & _MEDICAL_CUES:
            case["agencies"].append({"name": "Ambulance", "hotline": ref.agencies["Ambulance"]["hotline"], "reason": "medical cue"})
    case["flags"] = _flags(st, case)
    return case


def _flags(st: CallState, case: dict) -> list[str]:
    flags = []
    loc = case.get("location")
    if not loc:
        flags.append("location not yet identified")
    else:
        if loc["confidence"] < 0.6 and loc["status"] == "suggested":
            flags.append("location confidence low, confirm with caller")
        if loc["outside_coverage"]:
            flags.append("location outside gazetteer coverage")
        if loc["status"] == "suggested" and loc["source"] == "aml":
            flags.append("position from handset only, no spoken address")
    if any(not c.acked for c in st.cues.values()):
        flags.append("unacknowledged critical cues")
    if not case.get("incident"):
        flags.append("incident type not yet identified")
    return flags


__all__ = ["Reference", "build_case", "TAXONOMY"]
