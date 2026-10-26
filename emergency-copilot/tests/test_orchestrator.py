import json

import pytest

from copilot.models import Segment
from copilot.speech import load_scenarios, segments_for_turn


def play(orch, sid="en_cardiac", call_id="T1"):
    sc = load_scenarios()[sid]
    st = orch.start_call(call_id, sc["caller_number"], sc["aml"], sid)
    for turn in sc["turns"]:
        for seg in segments_for_turn(call_id, turn):
            orch.ingest(seg)
    return st


def test_ai_never_writes_cad_without_human_accept(orch):
    st = play(orch)
    assert st.fields["incident_type"].meta["type"] == "MEDICAL" and st.severity.level == "P1"
    assert orch.cad.records == {}
    orch.field_action("T1", "location", "accept")
    assert orch.cad.records["T1"]["fields"] == {"location": "Hamdan Street"}


def test_edit_reject_and_no_overwrite(orch):
    play(orch)
    orch.field_action("T1", "persons", "edit", "4")
    orch.field_action("T1", "injuries", "reject")
    orch.ingest(Segment(call_id="T1", speaker="caller", text="there are five people here", final=True))
    st = orch.calls["T1"]
    assert st.fields["persons"].value == "4" and st.fields["persons"].status == "edited"
    assert st.fields["persons"].meta["ai_alt"] == 5
    assert st.fields["injuries"].status == "rejected"
    assert orch.cad.records["T1"]["fields"]["persons"] == "4"
    with pytest.raises(ValueError):
        orch.field_action("T1", "persons", "edit", "")


def test_cues_raised_once_and_audited(orch):
    st = play(orch)
    assert {"not_breathing", "unconscious"} <= set(st.cues)
    assert sum(1 for r in orch.audit.read("T1") if r["action"] == "cue_raised") == len(st.cues)
    assert orch.ack_cue("T1", "not_breathing").acked


def test_sop_checklist_progresses(orch):
    st = orch.start_call("T2")
    assert st.next_question["id"] == "what"
    orch.ingest(Segment(call_id="T2", speaker="caller", text="my father is not breathing at Hamdan Street", final=True))
    done = {i.slot for i in orch.calls["T2"].checklist if i.done}
    assert {"incident", "location", "conscious_breathing"} <= done
    assert orch.calls["T2"].next_question["id"] in {"callback", "age", "problem", "persons"}
    assert any(i["cue"] == "not_breathing" for i in orch.calls["T2"].instructions)


def test_low_asr_confidence_lowers_field_confidence(orch):
    orch.start_call("A")
    orch.start_call("B")
    orch.ingest(Segment(call_id="A", text="fire at Hamdan Street", final=True, confidence=0.95))
    orch.ingest(Segment(call_id="B", text="fire at Hamdan Street", final=True, confidence=0.4))
    assert orch.calls["B"].fields["location"].confidence < orch.calls["A"].fields["location"].confidence


def test_shadow_mode_hides_ai_and_blocks_cad(orch):
    orch.set_mode("shadow")
    play(orch)
    pub = orch.public_state(orch.calls["T1"])
    assert pub["fields"] == {} and pub["cues"] == {} and pub["summary"] == ""
    assert orch.calls["T1"].fields  # still computed internally and logged
    with pytest.raises(PermissionError):
        orch.field_action("T1", "location", "accept")
    assert orch.wall()[0]["severity"] is None


def test_kill_switches(orch):
    for f in ("extraction", "location", "cues", "severity", "sop"):
        orch.set_feature(f, False)
    st = play(orch)
    assert not st.cues and not st.candidates and st.severity.level == "P4"
    assert len(st.segments) == 7  # transcript still flows: call-taker works as today


def test_end_call_archives_corrections(orch, tmp_path):
    play(orch)
    orch.field_action("T1", "persons", "edit", "3")
    orch.end_call("T1")
    rec = json.loads((tmp_path / "calls" / "T1.json").read_text(encoding="utf-8"))
    assert rec["corrections"] == [{"key": "persons", "ai_value": 2, "final": "3"}]
    with pytest.raises(ValueError):
        orch.ingest(Segment(call_id="T1", text="more", final=True))


def test_audit_chain_detects_tampering(orch, tmp_path):
    play(orch)
    assert orch.audit.verify()["ok"]
    p = tmp_path / "audit.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[3])
    rec["data"]["text"] = "tampered"
    lines[3] = json.dumps(rec, ensure_ascii=False)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert orch.audit.verify()["ok"] is False
    del lines[2]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert orch.audit.verify()["ok"] is False


def test_eval_harness_meets_mvp_targets():
    from copilot.eval import run
    r = run()
    assert r["pass"], r


def test_case_record_region_facilities_timeline(orch):
    from copilot.eval import replay
    sc = load_scenarios()
    st = replay(orch, sc["en_dhafra_gas"], "D1")
    case = orch.case(st)
    assert case["case_number"].startswith("AD-") and case["case_number"].endswith("00001")
    assert case["location"]["region"] == "Al Dhafra Region" and case["location"]["region_ar"] == "منطقة الظفرة"
    assert case["facilities"][0]["kind"] == "civil_defence" and case["facilities"][0]["distance_km"] < 5
    assert orch.calls["D1"].fields["region"].value == "Al Dhafra Region"
    st2 = replay(orch, sc["ar_alain_traffic"], "D2")
    c2 = orch.case(st2)
    assert c2["case_number"].endswith("00002") and c2["location"]["region"] == "Al Ain Region"
    assert {f["kind"] for f in c2["facilities"]} == {"police", "hospital"}  # traffic police + ambulance
    assert "unacknowledged critical cues" in c2["flags"]
    orch.field_action("D2", "location", "accept")
    events = [e["event"] for e in orch.case(orch.calls["D2"])["timeline"]]
    assert events[0] == "Call received" and any("accepted Location" in e for e in events)


def test_region_for_handset_only_and_outside_coverage(orch):
    st = orch.start_call("H1", aml={"lat": 23.66, "lon": 53.71, "accuracy_m": 50})
    orch.ingest(Segment(call_id="H1", speaker="caller", text="help there is a fire", final=True))
    loc = orch.case(st)["location"]
    assert loc["source"] == "aml" and loc["region"] == "Al Dhafra Region"
    assert any("handset only" in f for f in orch.case(st)["flags"])
    far = orch.ref.locate(26.5, 50.0)
    assert far.get("outside_coverage") is True


def test_case_number_survives_restart(tmp_path):
    from copilot.audit import AuditLog
    from copilot.cad import MockCAD
    from copilot.orchestrator import Orchestrator
    a = Orchestrator(AuditLog(tmp_path / "a.jsonl"), MockCAD(tmp_path / "c"))
    a.start_call("X1"); a.start_call("X2")
    b = Orchestrator(AuditLog(tmp_path / "a.jsonl"), MockCAD(tmp_path / "c"))
    assert b.start_call("X3").case_number.endswith("00003")


def test_shadow_mode_case_hides_ai_content(orch):
    orch.set_mode("shadow")
    st = play(orch)
    case = orch.public_state(st)["case"]
    assert "incident" not in case and "location" not in case
    assert all(not e["ai"] for e in case["timeline"]) and case["case_number"]
