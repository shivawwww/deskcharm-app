import time

import pytest
from fastapi.testclient import TestClient

from copilot.api import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as c:  # context manager keeps one event loop alive for background scenario tasks
        yield c


def test_manual_call_flow_and_ws(client):
    cid = client.post("/api/calls", json={"caller_number": "+9715"}).json()["call_id"]
    with client.websocket_connect(f"/ws/calls/{cid}") as ws:
        assert ws.receive_json()["state"]["call_id"] == cid
        r = client.post(f"/api/calls/{cid}/segments", json={"text": "fire at Hamdan Street, people trapped", "final": True})
        assert r.status_code == 200
        state = ws.receive_json()["state"]
        assert state["fields"]["incident_type"]["value"] == "Fire" and "trapped" in state["cues"]
    assert client.post(f"/api/calls/{cid}/fields/location", json={"action": "accept"}).json()["status"] == "accepted"
    assert client.post(f"/api/calls/{cid}/fields/location", json={"action": "bogus"}).status_code == 422
    assert client.post(f"/api/calls/{cid}/fields/nope", json={"action": "accept"}).status_code == 404
    assert client.post(f"/api/calls/{cid}/end").json()["status"] == "ended"
    assert client.post(f"/api/calls/{cid}/segments", json={"text": "x"}).status_code == 409
    assert client.get("/api/calls/missing").status_code == 404


def test_scenario_stream_and_wall(client):
    cid = client.post("/api/calls", json={"scenario": "ar_gulf_fire", "speed": 200}).json()["call_id"]
    for _ in range(100):
        s = client.get(f"/api/calls/{cid}").json()
        if len(s["segments"]) == 5:
            break
        time.sleep(0.05)
    assert s["severity"]["level"] == "P1" and s["dialect"] == "gulf" and s["code_switched"]
    assert client.get("/api/calls").json()[0]["location"] == "Sheikh Zayed bin Sultan Street"
    m = client.get("/api/metrics").json()
    assert m["processing_ms"]["count"] > 5 and m["processing_ms"]["p95"] < 800


def test_admin_audit_shadow_report(client):
    cid = client.post("/api/calls", json={"scenario": "en_cardiac", "speed": 200}).json()["call_id"]
    time.sleep(1.0)
    client.post(f"/api/calls/{cid}/truth", json={"incident_type": "MEDICAL", "location": "Hamdan Street", "cues": ["not_breathing"]})
    rep = client.get("/api/shadow/report").json()
    assert rep["incident_accuracy"] == 1.0 and rep["cue_recall"] == 1.0 and rep["gate_pass"] is True
    assert client.post("/api/admin/mode", json={"mode": "shadow"}).json() == {"mode": "shadow"}
    assert client.post(f"/api/calls/{cid}/fields/location", json={"action": "accept"}).status_code == 403
    assert client.post("/api/admin/features/cues", json={"enabled": False}).json()["cues"] is False
    assert client.post("/api/admin/features/zzz", json={"enabled": False}).status_code == 404
    assert client.get("/api/audit/verify").json()["ok"] is True
    actions = {r["action"] for r in client.get("/api/audit").json()}
    assert {"call_start", "transcript", "cue_raised", "mode_change", "feature_toggle", "shadow_truth"} <= actions
    assert len(client.get("/api/vocabulary").json()) > 30


def test_api_key_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_API_KEY", "s3cret")
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/metrics").status_code == 401
    assert c.get("/api/metrics", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert c.get("/api/health").status_code == 200
    with pytest.raises(Exception):
        with c.websocket_connect("/ws/wall"):
            pass


def test_case_regions_facilities_endpoints(client):
    cid = client.post("/api/calls", json={"scenario": "ar_alain_traffic", "speed": 200}).json()["call_id"]
    for _ in range(100):
        case = client.get(f"/api/calls/{cid}/case").json()
        if case.get("location") and len(case["timeline"]) > 6:
            break
        time.sleep(0.05)
    assert case["location"]["region"] == "Al Ain Region" and case["facilities"]
    assert client.get(f"/api/calls/{cid}").json()["case"]["case_number"] == case["case_number"]
    assert {r["name"] for r in client.get("/api/regions").json()} == {"Abu Dhabi Region", "Al Ain Region", "Al Dhafra Region"}
    assert {f["kind"] for f in client.get("/api/facilities").json()} == {"hospital", "police", "civil_defence"}
    assert client.get("/api/calls").json()[0]["region"] == "Al Ain Region"


def test_config_exposes_google_key_with_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("COPILOT_GOOGLE_MAPS_KEY", raising=False)
    with TestClient(create_app(tmp_path)) as c:
        assert c.get("/api/config").json()["google_maps_key"].startswith("AIza")  # default from data/maps.json
    monkeypatch.setenv("COPILOT_GOOGLE_MAPS_KEY", "")
    with TestClient(create_app(tmp_path)) as c:
        assert c.get("/api/config").json()["google_maps_key"] == ""  # empty disables Google Maps in the app
    monkeypatch.setenv("COPILOT_GOOGLE_MAPS_KEY", "custom-key")
    with TestClient(create_app(tmp_path)) as c:
        assert c.get("/api/config").json()["google_maps_key"] == "custom-key"
