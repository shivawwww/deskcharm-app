"""Shadow-mode scoring (governance gate): AI output vs what the call-taker actually entered, before any live use."""
from __future__ import annotations

from .orchestrator import Orchestrator

GATE = {"incident_accuracy": 0.80, "cue_recall": 0.95, "location_accuracy": 0.85}


def report(orch: Orchestrator) -> dict:
    inc_hit = inc_n = loc_hit = loc_n = cue_hit = cue_n = 0
    per_call = []
    for cid, truth in orch.truth.items():
        st = orch.calls.get(cid)
        if not st:
            continue
        row = {"call_id": cid}
        if "incident_type" in truth:
            ai = st.fields.get("incident_type")
            ok = bool(ai and ai.meta.get("type") == truth["incident_type"])
            inc_n, inc_hit, row["incident_ok"] = inc_n + 1, inc_hit + ok, ok
        if "location" in truth:
            ai = st.fields.get("location")
            ok = bool(ai and str(ai.value).lower() == str(truth["location"]).lower())
            loc_n, loc_hit, row["location_ok"] = loc_n + 1, loc_hit + ok, ok
        if "cues" in truth:
            want = set(truth["cues"])
            got = set(st.cues) & want
            cue_n, cue_hit = cue_n + len(want), cue_hit + len(got)
            row["cues_missed"] = sorted(want - got)
        per_call.append(row)
    rate = lambda h, n: round(h / n, 3) if n else None  # noqa: E731
    out = {"calls_scored": len(per_call), "incident_accuracy": rate(inc_hit, inc_n), "location_accuracy": rate(loc_hit, loc_n),
           "cue_recall": rate(cue_hit, cue_n), "per_call": per_call, "gate": GATE}
    metrics = [(k, out[k]) for k in GATE]
    out["gate_pass"] = bool(per_call) and all(v is not None and v >= GATE[k] for k, v in metrics)
    return out
