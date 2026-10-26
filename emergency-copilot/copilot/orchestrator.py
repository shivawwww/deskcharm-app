"""Call orchestrator: per-call state, merges AI outputs, applies confidence thresholds,
enforces human-in-the-loop, records every decision in the audit log.

Rules that matter for safety:
  * AI only *proposes*. A field reaches CAD only after the call-taker accepts or edits it.
  * Accepted / edited fields are never overwritten by later AI output (it is shown as an alternative).
  * Shadow mode computes and logs everything but shows nothing and writes nothing to CAD.
  * Every feature has a kill switch; with all AI features off the call-taker works exactly as today.
"""
from __future__ import annotations

import json
import time
import uuid
from collections import deque
from pathlib import Path

from .audit import AuditLog
from .bus import EventBus
from .casefile import Reference, build_case
from .cad import CADAdapter
from .extractor import Extraction, extract
from .lang import detect_lang
from .location import LocationEngine
from .models import CallState, Cue, FieldSuggestion, Segment
from .reasoner import TAXONOMY, Reasoner, RuleReasoner
from .sop import SopEngine

MIN_SHOW_CONF = 0.25
LOW_CONF = 0.5
FEATURES = ("extraction", "location", "cues", "severity", "sop", "cad_write")
_LABELS = {
    "incident_type": "Incident type", "location": "Location", "address_detail": "Building / floor", "priority": "Priority",
    "persons": "People involved", "ages": "Age(s)", "injuries": "Injuries", "weapons": "Weapons", "vehicles": "Vehicles",
    "plates": "Plate numbers", "hazards": "Hazards", "narrative": "Narrative", "caller_number": "Caller number",
    "language": "Caller language", "region": "Region", "narrative_ai": "Narrative (AI draft)",
}


class Orchestrator:
    def __init__(self, audit: AuditLog, cad: CADAdapter, bus: EventBus | None = None, location: LocationEngine | None = None,
                 sop: SopEngine | None = None, reasoner: Reasoner | None = None, mode: str = "assist",
                 archive_dir: Path | None = None):
        self.audit, self.cad = audit, cad
        self.bus = bus or EventBus()
        self.location = location or LocationEngine()
        self.sop = sop or SopEngine()
        self.reasoner = reasoner or RuleReasoner()
        self.mode = mode
        self.features = {f: True for f in FEATURES}
        self.archive_dir = Path(archive_dir) if archive_dir else None
        self.calls: dict[str, CallState] = {}
        self.truth: dict[str, dict] = {}
        self.latencies: deque[float] = deque(maxlen=5000)
        self.ex_cache: dict[str, Extraction] = {}
        self.ref = Reference(self.location.gaz.entries)
        self._case_seq = sum(1 for r in self.audit.read(limit=10**9) if r["action"] == "call_start")

    # ------------------------------------------------------------------ lifecycle
    def start_call(self, call_id: str | None = None, caller_number: str | None = None, aml: dict | None = None,
                   scenario: str | None = None) -> CallState:
        call_id = call_id or "C-" + uuid.uuid4().hex[:8]
        if call_id in self.calls:
            raise ValueError(f"call {call_id} already exists")
        self._case_seq += 1
        st = CallState(call_id=call_id, caller_number=caller_number, aml=aml, scenario=scenario,
                       case_number=f"AD-{time.strftime('%Y')}-{self._case_seq:05d}")
        self.calls[call_id] = st
        self._event(st, "Call received", ai=False)
        self.audit.write(call_id, "system", "call_start", {"case_number": st.case_number, "caller_number": caller_number, "aml": aml, "scenario": scenario, "mode": self.mode})
        if caller_number:
            self._upsert(st, FieldSuggestion(key="caller_number", label=_LABELS["caller_number"], value=caller_number, confidence=1.0,
                                             explanation="From telephony CTI metadata"))
        st.checklist = self.sop.checklist("UNKNOWN", self._filled(st, Extraction()))
        st.next_question = self.sop.next_question(st.checklist)
        self._emit(st)
        return st

    def end_call(self, call_id: str) -> CallState:
        st = self._get(call_id)
        if st.status == "ended":
            return st
        st.status, st.ended_at = "ended", time.time()
        self._event(st, "Call ended", ai=False)
        st.partials = {}
        if self.features["cad_write"] and self.mode == "assist":
            self.cad.close_incident(call_id)
        self.audit.write(call_id, "system", "call_end", {"fields": {k: [f.status, f.value] for k, f in st.fields.items()}})
        self._archive(st)
        self._emit(st)
        return st

    # --------------------------------------------------------------------- ingest
    def ingest(self, seg: Segment) -> CallState:
        t0 = time.perf_counter()
        st = self._get(seg.call_id)
        if st.status != "active":
            raise ValueError("call has ended")
        st.seq += 1
        seg.seq = st.seq
        if seg.speaker != "calltaker":
            li = detect_lang(seg.text)
            seg.lang, seg.dialect, seg.code_switched = li.lang, li.dialect, li.code_switched
        if seg.final:
            st.segments.append(seg)
            st.partials.pop(seg.speaker, None)
            self.audit.write(st.call_id, seg.speaker, "transcript", {"text": seg.text, "confidence": seg.confidence, "lang": seg.lang, "dialect": seg.dialect})
        else:
            st.partials[seg.speaker] = seg
        st.last_activity = time.time()
        if seg.speaker != "calltaker":
            self._analyse(st, final=seg.final)
        st.timings_ms["total"] = round((time.perf_counter() - t0) * 1000, 2)
        self.latencies.append(st.timings_ms["total"])
        self._emit(st)
        return st

    def _caller_segments(self, st: CallState) -> list[Segment]:
        segs = [s for s in st.segments if s.speaker != "calltaker"]
        segs += [s for s in st.partials.values() if s.speaker != "calltaker"]
        return segs

    def _analyse(self, st: CallState, final: bool) -> None:
        tm = st.timings_ms
        segs = self._caller_segments(st)
        text = " . ".join(s.text for s in segs)
        li = detect_lang(text)  # aggregate over the call so one marker-free sentence does not reset the dialect
        if li.lang != "unknown":
            st.lang, st.dialect, st.code_switched = li.lang, li.dialect, li.code_switched
        asr = sum(s.confidence for s in segs) / max(1, len(segs))
        asr_factor = 0.5 + 0.5 * asr

        t = time.perf_counter()
        ex = extract(text) if self.features["extraction"] else Extraction()
        tm["extract"] = round((time.perf_counter() - t) * 1000, 2)
        self.ex_cache[st.call_id] = ex

        new_cue = False
        if self.features["cues"]:
            for c in ex.cues:
                if c.id not in st.cues:
                    st.cues[c.id] = Cue(id=c.id, label=c.label, level=c.level, evidence=c.evidence)
                    self.audit.write(st.call_id, "ai", "cue_raised", {"cue": c.id, "evidence": c.evidence})
                    self._event(st, f"Critical cue: {c.label}", ai=True)
                    new_cue = True

        t = time.perf_counter()
        if self.features["location"]:
            st.candidates = self.location.resolve(" . ".join(s.text for s in segs), st.aml)
        tm["location"] = round((time.perf_counter() - t) * 1000, 2)

        self._propose_entities(st, ex, asr_factor)

        t = time.perf_counter()
        if (final or new_cue) and self.features["severity"] and self.features["extraction"]:
            st.severity = self.reasoner.severity(ex)
            best = st.candidates[0] if st.candidates else None
            detail = ex.address_detail
            st.summary = self.reasoner.summary(ex, best, detail)
            conf = round(0.4 + 0.5 * ex.incident_conf, 2) if ex.incident_type != "UNKNOWN" else 0.3
            self._upsert(st, FieldSuggestion(
                key="priority", label=_LABELS["priority"], value=f"{st.severity.level} {st.severity.name}", confidence=conf,
                evidence=[r.evidence for r in st.severity.reasons if r.evidence],
                explanation="; ".join(f"{r.label} (+{r.points})" for r in st.severity.reasons), meta={"score": st.severity.score}))
            self._upsert(st, FieldSuggestion(key="narrative", label=_LABELS["narrative"], value=st.summary, confidence=0.7,
                                             explanation="Generated from extracted facts"))
        tm["reason"] = round((time.perf_counter() - t) * 1000, 2)

        t = time.perf_counter()
        if self.features["sop"]:
            filled = self._filled(st, ex)
            st.checklist = self.sop.checklist(ex.incident_type if ex.incident_conf >= LOW_CONF else "UNKNOWN", filled)
            st.next_question = self.sop.next_question(st.checklist, st.lang)
            st.instructions = self.sop.instructions_for(set(st.cues))
        tm["sop"] = round((time.perf_counter() - t) * 1000, 2)

    def _filled(self, st: CallState, ex: Extraction) -> set[str]:
        filled = set(ex.answered)
        if ex.incident_type != "UNKNOWN" and ex.incident_conf >= LOW_CONF:
            filled.add("incident")
        if st.candidates and st.candidates[0].confidence >= 0.6 and st.candidates[0].source == "speech":
            filled.add("location")
        if st.caller_number:
            filled.add("callback")
        if ex.persons:
            filled.add("persons")
        if ex.ages:
            filled.add("age")
        if ex.injuries:
            filled.add("injuries")
        if ex.vehicles:
            filled.add("vehicles")
        if ex.hazards:
            filled.add("hazards")
        for k in ("location", "incident_type", "injuries", "persons"):
            f = st.fields.get(k)
            if f and f.status in ("accepted", "edited"):
                filled.add({"incident_type": "incident"}.get(k, k))
        return filled

    def _propose_entities(self, st: CallState, ex: Extraction, f: float) -> None:
        P = lambda key, value, conf, ev=(), expl="", meta=None: self._upsert(  # noqa: E731
            st, FieldSuggestion(key=key, label=_LABELS[key], value=value, confidence=round(min(0.99, conf), 2),
                                evidence=[e for e in ev if e], explanation=expl, meta=meta or {}))
        if ex.incident_type != "UNKNOWN":
            tx = TAXONOMY[ex.incident_type]
            P("incident_type", tx["label"], ex.incident_conf * f, ex.type_evidence, "Keywords: " + ", ".join(ex.type_evidence),
              {"code": tx["code"], "agencies": tx["agencies"], "type": ex.incident_type})
        if st.candidates:
            b = st.candidates[0]
            conf = b.confidence * (f if b.source == "speech" else 1.0)
            P("location", b.name, conf, [b.evidence], b.note or ("Matched against local gazetteer" if b.source == "speech" else "Handset position only"),
              {"lat": b.lat, "lon": b.lon, "candidates": [c.model_dump() for c in st.candidates]})
            where = self.ref.locate(b.lat, b.lon)
            reg = b.region or where["region"]
            r = self.ref.region(reg)
            P("region", reg, conf * 0.95, [b.evidence], "Derived from the matched place" if b.region else "Nearest known place to the handset position",
              {"name_ar": r["name_ar"] if r else None, "area": b.area or where["area"]})
        if ex.address_detail:
            P("address_detail", ", ".join(ex.address_detail), 0.8 * f, [], "Spoken building / floor details")
        if ex.persons:
            P("persons", ex.persons, 0.85 * f, [ex.persons_evidence])
        if ex.ages:
            P("ages", ", ".join(str(a) for a in ex.ages), 0.85 * f, ex.ages_evidence)
        if ex.injuries:
            P("injuries", ", ".join(ex.injuries), 0.8 * f, [])
        if ex.weapons:
            P("weapons", ", ".join(dict.fromkeys(ex.weapons)), 0.8 * f, ex.weapons)
        if ex.vehicles:
            P("vehicles", ", ".join(ex.vehicles), 0.8 * f)
        if ex.plates:
            P("plates", ", ".join(ex.plates), 0.6 * f, [], "Digits are error-prone: read back to the caller")
        if ex.hazards:
            P("hazards", ", ".join(ex.hazards), 0.8 * f)
        if st.lang != "unknown":
            P("language", st.lang + (f" / {st.dialect}" if st.dialect else "") + (" (code-switched)" if st.code_switched else ""), 0.75)

    def _upsert(self, st: CallState, p: FieldSuggestion) -> bool:
        if p.confidence < MIN_SHOW_CONF:
            return False
        cur = st.fields.get(p.key)
        if cur:
            if cur.status in ("accepted", "edited"):
                if p.value != cur.value:
                    cur.meta["ai_alt"] = p.value
                return False
            if cur.status == "rejected" and cur.value == p.value:
                return False
            if cur.value == p.value and abs(cur.confidence - p.confidence) < 0.05:
                return False
        value_changed = cur is None or cur.value != p.value
        st.fields[p.key] = p
        if cur is None and p.key in ("incident_type", "location", "region"):
            self._event(st, f"{p.label} suggested: {p.value}", ai=True)
        if value_changed:
            self.audit.write(st.call_id, "ai", "suggestion", {"key": p.key, "value": p.value, "confidence": p.confidence, "evidence": p.evidence})
        return True

    # ---------------------------------------------------------- human decisions
    def field_action(self, call_id: str, key: str, action: str, value=None, actor: str = "calltaker") -> FieldSuggestion:
        if self.mode == "shadow":
            raise PermissionError("shadow mode: AI output is not available to call-takers")
        st = self._get(call_id)
        f = st.fields.get(key)
        if f is None:
            raise KeyError(key)
        old = f.value
        if action == "accept":
            f.status = "accepted"
        elif action == "reject":
            f.status = "rejected"
        elif action == "edit":
            if value in (None, ""):
                raise ValueError("edit needs a value")
            f.value, f.status, f.confidence = value, "edited", 1.0
        else:
            raise ValueError(f"unknown action {action}")
        f.updated_at = time.time()
        self._event(st, f"{actor} {dict(accept='accepted', reject='rejected', edit='edited')[action]} {f.label}: {f.value}", ai=False)
        self.audit.write(call_id, actor, f"field_{action}", {"key": key, "value": f.value, "previous": old, "confidence": f.confidence})
        if f.status in ("accepted", "edited") and self.features["cad_write"]:
            self.cad.update_field(call_id, key, f.value, f.meta)
            self.audit.write(call_id, "system", "cad_write", {"key": key, "value": f.value})
        self._emit(st)
        return f

    def ack_cue(self, call_id: str, cue_id: str, actor: str = "calltaker") -> Cue:
        st = self._get(call_id)
        cue = st.cues[cue_id]
        cue.acked = True
        self.audit.write(call_id, actor, "cue_ack", {"cue": cue_id})
        self._emit(st)
        return cue

    # -------------------------------------------------------------------- admin
    def set_feature(self, name: str, enabled: bool, actor: str = "supervisor") -> dict:
        if name not in self.features:
            raise KeyError(name)
        self.features[name] = enabled
        self.audit.write(None, actor, "feature_toggle", {"feature": name, "enabled": enabled})
        return self.features

    def set_mode(self, mode: str, actor: str = "supervisor") -> str:
        if mode not in ("shadow", "assist"):
            raise ValueError(mode)
        self.mode = mode
        self.audit.write(None, actor, "mode_change", {"mode": mode})
        for st in self.calls.values():
            self._emit(st)
        return mode

    # ------------------------------------------------------------------ queries
    def _get(self, call_id: str) -> CallState:
        if call_id not in self.calls:
            raise KeyError(call_id)
        return self.calls[call_id]

    def _event(self, st: CallState, text: str, ai: bool) -> None:
        now = time.time()
        st.timeline.append({"ts": round(now, 2), "t_s": round(now - st.started_at, 1), "event": text, "ai": ai})

    def case(self, st: CallState) -> dict:
        return build_case(st, self.ref, include_ai=self.mode != "shadow")

    def public_state(self, st: CallState) -> dict:
        d = st.model_dump()
        d["case"] = self.case(st)
        d.pop("timeline", None)
        d["mode"] = self.mode
        d["features"] = self.features
        if self.mode == "shadow":
            d.update(fields={}, cues={}, candidates=[], severity={"score": 0, "level": "-", "name": "hidden (shadow mode)", "reasons": []},
                     summary="", checklist=[], next_question=None, instructions=[])
        return d

    def wall(self) -> list[dict]:
        now = time.time()
        rows = []
        for st in self.calls.values():
            hidden = self.mode == "shadow"
            loc = st.fields.get("location")
            inc = st.fields.get("incident_type")
            rows.append({
                "call_id": st.call_id, "status": st.status, "age_s": int((st.ended_at or now) - st.started_at),
                "silent_s": int(now - st.last_activity) if st.status == "active" else 0,
                "stuck": st.status == "active" and now - st.last_activity > 30,
                "lang": st.lang, "dialect": st.dialect, "case_number": st.case_number,
                "region": None if hidden or not st.fields.get("region") else st.fields["region"].value,
                "severity": None if hidden else {"level": st.severity.level, "score": st.severity.score},
                "incident": None if hidden or not inc else inc.value, "location": None if hidden or not loc else loc.value,
                "cues": [] if hidden else [{"id": c.id, "label": c.label, "acked": c.acked} for c in st.cues.values()],
            })
        order = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}
        rows.sort(key=lambda r: (r["status"] != "active", order.get((r["severity"] or {}).get("level"), 9), r["age_s"]))
        return rows

    def metrics(self) -> dict:
        lat = sorted(self.latencies)

        def pct(p: float) -> float | None:
            return lat[min(len(lat) - 1, int(p * len(lat)))] if lat else None

        decisions = {"accepted": 0, "edited": 0, "rejected": 0}
        for st in self.calls.values():
            for f in st.fields.values():
                if f.status in decisions:
                    decisions[f.status] += 1
        decided = sum(decisions.values())
        return {
            "processing_ms": {"count": len(lat), "p50": pct(0.5), "p95": pct(0.95), "max": lat[-1] if lat else None,
                              "note": "In-process compute only (extraction, geocoding, rules, state merge). ASR and network add ~350 ms in the 800 ms budget."},
            "field_decisions": decisions,
            "field_acceptance_rate": round((decisions["accepted"]) / decided, 3) if decided else None,
            "active_calls": sum(1 for s in self.calls.values() if s.status == "active"),
            "mode": self.mode, "features": self.features,
        }

    # ------------------------------------------------------------------ plumbing
    def _emit(self, st: CallState) -> None:
        self.bus.publish(f"call:{st.call_id}", {"type": "state", "state": self.public_state(st)})
        self.bus.publish("wall", {"type": "wall", "calls": self.wall()})

    def _archive(self, st: CallState) -> None:
        """Collect step of the learning loop: transcript, AI outputs and every correction, for QA and fine-tuning."""
        if not self.archive_dir:
            return
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        corrections = [{"key": k, "ai_value": next((r["data"].get("previous") for r in self.audit.read(st.call_id) if r["action"] == "field_edit" and r["data"]["key"] == k), None),
                        "final": f.value} for k, f in st.fields.items() if f.status == "edited"]
        rec = {"call": st.model_dump(), "corrections": corrections}
        (self.archive_dir / f"{st.call_id}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
