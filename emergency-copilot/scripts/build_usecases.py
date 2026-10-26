"""Build web/usecases.html: a self-contained static page. Demo frames are produced by the real pipeline,
so the page cannot drift from what the Co-Pilot actually extracts.

    python scripts/build_usecases.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from copilot.audit import AuditLog  # noqa: E402
from copilot.cad import MockCAD  # noqa: E402
from copilot.extractor import extract  # noqa: E402
from copilot.lang import detect_lang  # noqa: E402
from copilot.models import Segment  # noqa: E402
from copilot.orchestrator import Orchestrator  # noqa: E402
from copilot.speech import load_scenarios  # noqa: E402

KEYS = ("incident_type", "priority", "location", "region", "address_detail", "persons", "ages", "injuries", "hazards")


def frames_for(orch: Orchestrator, sc: dict) -> list[dict]:
    cid = "DEMO-" + sc["id"]
    st = orch.start_call(cid, sc.get("caller_number"), sc.get("aml"), sc["id"])
    out = []
    for turn in sc["turns"]:
        orch.ingest(Segment(call_id=cid, speaker=turn["speaker"], text=turn["text"], final=True, confidence=turn.get("confidence", 0.92)))
        case = orch.case(st)
        q = st.next_question
        out.append({
            "speaker": turn["speaker"], "text": turn["text"], "lowq": turn.get("confidence", 1) < 0.7,
            "cues": [{"id": c.id, "label": c.label, "level": c.level, "evidence": c.evidence} for c in st.cues.values()],
            "fields": {k: {"label": st.fields[k].label, "value": str(st.fields[k].value), "conf": st.fields[k].confidence} for k in KEYS if k in st.fields},
            "priority": {"level": st.severity.level, "score": st.severity.score, "reasons": [f"{r.label} +{r.points}" for r in st.severity.reasons]},
            "next": {"en": q["en"], "ar": q["ar"]} if q else None,
            "type": st.fields["incident_type"].meta.get("type") if "incident_type" in st.fields else None,
            "instr": [{"cue": i["cue"], "en": i["en"], "ar": i["ar"]} for i in st.instructions],
            "lang": f"{st.lang}{'/' + st.dialect if st.dialect else ''}{' · code-switched' if st.code_switched else ''}",
            "checklist": [{"q": i.question["en"], "done": i.done} for i in st.checklist],
            "case": {"number": case["case_number"], "region": (case.get("location") or {}).get("region"), "area": (case.get("location") or {}).get("area"),
                     "agencies": case.get("agencies", []), "facilities": [{"name": f["name"], "kind": f["kind"], "km": f["distance_km"]} for f in case.get("facilities", [])]},
        })
    return out


def audio_samples(orch: Orchestrator) -> list[dict]:
    """Embed the clips and run the real extractor on each transcript, so unsupported languages show as unsupported."""
    out = []
    for smp in json.loads((ROOT / "copilot" / "data" / "audio_samples.json").read_text(encoding="utf-8"))["samples"]:
        ex = extract(smp["text"])
        loc = orch.location.resolve(smp["text"])
        mp3 = (ROOT / "web" / "audio" / f"{smp['id']}.mp3").read_bytes()
        out.append({**{k: smp[k] for k in ("id", "name", "native", "dir", "text")},
                    "src": "data:audio/mpeg;base64," + base64.b64encode(mp3).decode(), "kb": len(mp3) // 1024,
                    "lang": detect_lang(smp["text"]).lang, "incident": ex.incident_type, "conf": ex.incident_conf,
                    "cues": [c.label for c in ex.cues], "location": loc[0].name if loc else None})
    return out


def pin_for(orch: Orchestrator, call_id: str) -> dict | None:
    loc = orch.case(orch.calls[call_id]).get("location")
    return {"name": loc["name"], "lat": loc["lat"], "lon": loc["lon"], "region": loc["region"]} if loc else None


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    orch = Orchestrator(AuditLog(tmp / "a.jsonl"), MockCAD(tmp / "c"))
    scen = load_scenarios()
    data = {
        "scenarios": {sid: {"id": sid, "title": sc["title"], "aml": sc.get("aml"), "frames": frames_for(orch, sc), "pin": pin_for(orch, "DEMO-" + sid)} for sid, sc in scen.items()},
        "places": [{k: e[k] for k in ("name", "name_ar", "type", "lat", "lon", "region")} for e in orch.location.gaz.entries],
        "audio": audio_samples(orch), "facilities": orch.ref.facilities, "regions": orch.ref.regions,
    }
    data["agencies"] = orch.ref.agencies
    uc = json.loads((ROOT / "copilot" / "data" / "usecases.json").read_text(encoding="utf-8"))
    data["usecases"], data["ui"], data["cats"] = uc["usecases"], uc["ui"], uc["cats"]
    art = (ROOT / "web" / "art.js").read_text(encoding="utf-8")
    gkey = json.dumps(os.environ.get("COPILOT_GOOGLE_MAPS_KEY", json.loads((ROOT / "copilot" / "data" / "maps.json").read_text(encoding="utf-8"))["google_maps_key"]))
    data["help"] = json.loads((ROOT / "copilot" / "data" / "help.json").read_text(encoding="utf-8"))["entries"]
    widget = (ROOT / "web" / "help_widget.html").read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for name in ("usecases", "ccc"):
        tpl_path = ROOT / "web" / f"{name}.template.html"
        if not tpl_path.exists():
            continue
        d = data if name == "usecases" else {k: v for k, v in data.items() if k not in ("usecases", "ui", "cats", "audio", "help_unused")}
        html = tpl_path.read_text(encoding="utf-8").replace("/*__DATA__*/null", json.dumps(d, ensure_ascii=False, separators=(",", ":"))).replace("<!--__HELP__-->", widget).replace("/*__ART__*/", art).replace("/*__GMAPS_KEY__*/\"\"", gkey)
        (ROOT / "web" / f"{name}.html").write_text(html, encoding="utf-8")
        print(f"web/{name}.html written ({len(html) // 1024} KB, {len(data['scenarios'])} scenarios)")


if __name__ == "__main__":
    main()
