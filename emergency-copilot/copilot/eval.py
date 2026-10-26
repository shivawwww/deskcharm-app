"""Offline evaluation harness: replays every scenario and scores it against MVP KPI targets.

    python -m copilot.eval
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .audit import AuditLog
from .cad import MockCAD
from .orchestrator import Orchestrator
from .speech import load_scenarios, segments_for_turn

TARGETS = {"incident_accuracy": 0.80, "cue_recall": 0.95, "location_accuracy": 0.85, "p95_processing_ms": 800.0}


def replay(orch: Orchestrator, sc: dict, call_id: str):
    st = orch.start_call(call_id, sc.get("caller_number"), sc.get("aml"), sc["id"])
    for turn in sc["turns"]:
        for seg in segments_for_turn(call_id, turn):
            orch.ingest(seg)
    return st


def run() -> dict:
    tmp = Path(tempfile.mkdtemp())
    orch = Orchestrator(AuditLog(tmp / "audit.jsonl"), MockCAD(tmp / "cad"))
    inc = loc = reg = cue_hit = cue_n = extra_cues = 0
    rows = []
    scenarios = load_scenarios()
    for sid, sc in scenarios.items():
        st = replay(orch, sc, f"EVAL-{sid}")
        exp = sc["expected"]
        f_inc, f_loc = st.fields.get("incident_type"), st.fields.get("location")
        ok_inc = bool(f_inc and f_inc.meta["type"] == exp["incident_type"])
        ok_loc = bool(f_loc and f_loc.value == exp["location"])
        ok_reg = bool(st.fields.get("region") and st.fields["region"].value == exp["region"])
        reg += ok_reg
        got, want = set(st.cues), set(exp["cues"])
        inc, loc, cue_hit, cue_n, extra_cues = inc + ok_inc, loc + ok_loc, cue_hit + len(got & want), cue_n + len(want), extra_cues + len(got - want)
        rows.append((sid, ok_inc, ok_loc, sorted(want - got), sorted(got - want), st.severity.level))
    n = len(scenarios)
    lat = sorted(orch.latencies)
    res = {"region_accuracy": reg / n, "incident_accuracy": inc / n, "location_accuracy": loc / n, "cue_recall": cue_hit / cue_n,
           "p95_processing_ms": lat[int(0.95 * (len(lat) - 1))], "unexpected_cues": extra_cues, "rows": rows}
    res["pass"] = (res["incident_accuracy"] >= TARGETS["incident_accuracy"] and res["cue_recall"] >= TARGETS["cue_recall"]
                   and res["location_accuracy"] >= TARGETS["location_accuracy"] and res["p95_processing_ms"] <= TARGETS["p95_processing_ms"])
    return res


if __name__ == "__main__":
    r = run()
    print(f"{'scenario':20} incident location missed-cues  extra-cues  priority")
    for sid, a, b, miss, extra, pr in r["rows"]:
        print(f"{sid:20} {str(a):8} {str(b):8} {miss!s:12} {extra!s:11} {pr}")
    print(f"\nincident accuracy {r['incident_accuracy']:.0%} | location {r['location_accuracy']:.0%} | cue recall {r['cue_recall']:.0%} "
          f"| region {r['region_accuracy']:.0%} | p95 processing {r['p95_processing_ms']:.1f} ms | PASS={r['pass']}")
    print("NOTE: scripted scenarios only. Real KPIs must be baselined on the client's own audio in the POC.")
    sys.exit(0 if r["pass"] else 1)
