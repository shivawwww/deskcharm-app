"""HTTP / WebSocket surface: call-taker panel, supervisor wall, admin, audit, ASR gateway."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import shadow
from .agent import SupportAgent
from .aisummary import draft_summary
from .audit import AuditLog
from .cad import CADAdapter, MockCAD, RestCAD
from .llm import LLMConfig, LLMError, build_llm
from .models import Segment
from .orchestrator import Orchestrator
from .speech import ASRBackend, ScriptedASR, load_scenarios

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


class StartCall(BaseModel):
    scenario: str | None = None
    caller_number: str | None = None
    aml: dict[str, float] | None = None
    speed: float = 3.0


class SegmentIn(BaseModel):
    speaker: str = "caller"
    text: str
    final: bool = False
    confidence: float = 0.9


class FieldAction(BaseModel):
    action: str
    value: Any = None


class Toggle(BaseModel):
    enabled: bool


class ModeIn(BaseModel):
    mode: str


def _google_key() -> str:
    """Env COPILOT_GOOGLE_MAPS_KEY wins (empty string disables Google Maps); otherwise the default in data/maps.json."""
    if "COPILOT_GOOGLE_MAPS_KEY" in os.environ:
        return os.environ["COPILOT_GOOGLE_MAPS_KEY"].strip()
    return json.loads((Path(__file__).parent / "data" / "maps.json").read_text(encoding="utf-8")).get("google_maps_key", "")


class ChatIn(BaseModel):
    messages: list[dict[str, Any]]


def _default_asr() -> ASRBackend | None:
    if os.environ.get("COPILOT_ASR", "off").lower() != "whisper":
        return None
    from .speech import FasterWhisperBackend  # optional dependency, loads the model once

    return FasterWhisperBackend(os.environ.get("COPILOT_WHISPER_MODEL", "small"), os.environ.get("COPILOT_WHISPER_DEVICE", "auto"))


def create_app(var_dir: Path | None = None, cad: CADAdapter | None = None, mode: str | None = None,
               asr_backend: ASRBackend | None = None, llm: Any = None, llm_cfg: LLMConfig | None = None) -> FastAPI:
    var = Path(var_dir or os.environ.get("COPILOT_VAR", ROOT / "var"))
    if cad is None:
        cad = RestCAD(os.environ["CAD_URL"]) if os.environ.get("CAD_URL") else MockCAD(var / "cad")
    orch = Orchestrator(AuditLog(var / "audit.jsonl"), cad, mode=mode or os.environ.get("COPILOT_MODE", "assist"), archive_dir=var / "calls")
    scenarios = load_scenarios()
    llm_cfg = llm_cfg or LLMConfig.from_env()
    if llm is None:
        llm = build_llm(llm_cfg)
    if asr_backend is None:
        asr_backend = _default_asr()
    agent = SupportAgent(orch, llm, llm_cfg)
    api_key = os.environ.get("COPILOT_API_KEY")

    def auth(x_api_key: str | None = Header(default=None)) -> None:
        if api_key and x_api_key != api_key:
            raise HTTPException(401, "invalid API key")

    app = FastAPI(title="Emergency Call Co-Pilot", version="0.1.0")
    app.state.orch, app.state.tasks = orch, {}
    protected = [Depends(auth)]

    def call_or_404(call_id: str):
        try:
            return orch._get(call_id)
        except KeyError:
            raise HTTPException(404, "unknown call") from None

    @app.get("/api/health")
    def health():
        return {"ok": True, "mode": orch.mode, "audit": orch.audit.verify()["ok"]}

    @app.get("/api/scenarios", dependencies=protected)
    def list_scenarios():
        return [{"id": s["id"], "title": s["title"]} for s in scenarios.values()]

    @app.post("/api/calls", dependencies=protected)
    async def start_call(body: StartCall):
        sc = scenarios.get(body.scenario) if body.scenario else None
        if body.scenario and not sc:
            raise HTTPException(404, "unknown scenario")
        st = orch.start_call(caller_number=body.caller_number or (sc or {}).get("caller_number"),
                             aml=body.aml or (sc or {}).get("aml"), scenario=body.scenario)
        if sc:
            async def run():
                async for seg in ScriptedASR(sc, speed=body.speed).stream(st.call_id):
                    if orch.calls[st.call_id].status != "active":
                        return
                    orch.ingest(seg)
            app.state.tasks[st.call_id] = asyncio.create_task(run())
        return {"call_id": st.call_id}

    @app.get("/api/calls", dependencies=protected)
    def wall():
        return orch.wall()

    @app.get("/api/calls/{call_id}", dependencies=protected)
    def get_call(call_id: str):
        return orch.public_state(call_or_404(call_id))

    @app.get("/api/calls/{call_id}/case", dependencies=protected)
    def get_case(call_id: str):
        return orch.case(call_or_404(call_id))

    @app.get("/api/agent/status", dependencies=protected)
    def agent_status():
        return agent.status()

    @app.post("/api/agent/chat", dependencies=protected)
    async def agent_chat(body: ChatIn):
        try:
            return await asyncio.to_thread(agent.chat, body.messages)  # LLM calls are slow: keep the event loop free for live calls
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/api/calls/{call_id}/ai-summary", dependencies=protected)
    async def ai_summary(call_id: str):
        call_or_404(call_id)
        try:
            sug = await asyncio.to_thread(draft_summary, orch, llm, llm_cfg, call_id)
        except LLMError as e:
            raise HTTPException(409, str(e)) from None
        except Exception as e:
            raise HTTPException(502, f"AI service error: {type(e).__name__}") from None
        return sug

    @app.post("/api/transcribe", dependencies=protected)
    async def transcribe(request: Request, language: str | None = None):
        """Raw audio bytes in the body (wav/mp3/ogg); local Whisper only. For checking real recordings against the ASR."""
        if asr_backend is None or not hasattr(asr_backend, "transcribe_file"):
            raise HTTPException(501, "no local ASR configured: pip install faster-whisper and set COPILOT_ASR=whisper")
        data = await request.body()
        if not data or len(data) > 25_000_000:
            raise HTTPException(413, "send 1 byte to 25 MB of audio in the request body")
        with tempfile.NamedTemporaryFile(suffix=".audio") as f:
            f.write(data)
            f.flush()
            text, conf, lang = await asr_backend.transcribe_file(f.name, language, orch.location.gaz.hotwords())
        orch.audit.write(None, "system", "transcribe_file", {"bytes": len(data), "language": lang})
        return {"text": text, "confidence": conf, "language": lang}

    @app.get("/api/regions", dependencies=protected)
    def regions():
        return orch.ref.regions

    @app.get("/api/facilities", dependencies=protected)
    def facilities():
        return orch.ref.facilities

    @app.post("/api/calls/{call_id}/segments", dependencies=protected)
    def post_segment(call_id: str, body: SegmentIn):
        call_or_404(call_id)
        if body.speaker not in ("caller", "calltaker", "third_party"):
            raise HTTPException(422, "bad speaker")
        try:
            orch.ingest(Segment(call_id=call_id, speaker=body.speaker, text=body.text, final=body.final, confidence=body.confidence))
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        return {"ok": True}

    @app.post("/api/calls/{call_id}/fields/{key}", dependencies=protected)
    def field_action(call_id: str, key: str, body: FieldAction):
        try:
            return orch.field_action(call_id, key, body.action, body.value)
        except KeyError:
            raise HTTPException(404, "unknown call or field") from None
        except PermissionError as e:
            raise HTTPException(403, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/api/calls/{call_id}/cues/{cue_id}/ack", dependencies=protected)
    def ack(call_id: str, cue_id: str):
        try:
            return orch.ack_cue(call_id, cue_id)
        except KeyError:
            raise HTTPException(404, "unknown call or cue") from None

    @app.post("/api/calls/{call_id}/end", dependencies=protected)
    def end(call_id: str):
        call_or_404(call_id)
        task = app.state.tasks.pop(call_id, None)
        if task:
            task.cancel()
        return orch.public_state(orch.end_call(call_id))

    @app.post("/api/calls/{call_id}/truth", dependencies=protected)
    def truth(call_id: str, body: dict[str, Any]):
        """Shadow mode: what the call-taker actually recorded in CAD (incident_type, location, cues)."""
        call_or_404(call_id)
        orch.truth[call_id] = body
        orch.audit.write(call_id, "calltaker", "shadow_truth", body)
        return {"ok": True}

    @app.get("/api/shadow/report", dependencies=protected)
    def shadow_report():
        return shadow.report(orch)

    @app.get("/api/metrics", dependencies=protected)
    def metrics():
        return orch.metrics()

    @app.get("/api/vocabulary", dependencies=protected)
    def vocabulary():
        return orch.location.gaz.hotwords()

    @app.get("/api/config")
    def config():
        """Map settings. Point COPILOT_TILE_URL at an on-premise tile server for air-gapped centres."""
        return {"tile_url": os.environ.get("COPILOT_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
                "attribution": os.environ.get("COPILOT_TILE_ATTRIBUTION", "© OpenStreetMap contributors"),
                "center": [24.4539, 54.3773], "zoom": 11, "google_maps_key": _google_key()}

    @app.get("/api/gazetteer", dependencies=protected)
    def gazetteer():
        return [{k: e[k] for k in ("id", "name", "name_ar", "type", "lat", "lon", "region", "area")} for e in orch.location.gaz.entries]

    @app.get("/api/audit", dependencies=protected)
    def audit(call_id: str | None = None, limit: int = 300):
        return orch.audit.read(call_id, limit)

    @app.get("/api/audit/verify", dependencies=protected)
    def audit_verify():
        return orch.audit.verify()

    @app.get("/api/admin/features", dependencies=protected)
    def features():
        return {"mode": orch.mode, "features": orch.features}

    @app.post("/api/admin/features/{name}", dependencies=protected)
    def toggle(name: str, body: Toggle):
        try:
            return orch.set_feature(name, body.enabled)
        except KeyError:
            raise HTTPException(404, "unknown feature") from None

    @app.post("/api/admin/mode", dependencies=protected)
    def set_mode(body: ModeIn):
        try:
            return {"mode": orch.set_mode(body.mode)}
        except ValueError:
            raise HTTPException(422, "mode must be shadow or assist") from None

    # --------------------------------------------------------------- websockets
    async def ws_guard(ws: WebSocket, token: str | None) -> bool:
        if api_key and token != api_key:
            await ws.close(code=4401)
            return False
        await ws.accept()
        return True

    async def pump(ws: WebSocket, topic: str):
        q = orch.bus.subscribe(topic)
        try:
            for evt in orch.bus.replay(topic, 1):
                await ws.send_json(evt)
            while True:
                await ws.send_json(await q.get())
        except WebSocketDisconnect:
            pass
        finally:
            orch.bus.unsubscribe(topic, q)

    @app.websocket("/ws/calls/{call_id}")
    async def ws_call(ws: WebSocket, call_id: str, token: str | None = Query(default=None)):
        if not await ws_guard(ws, token):
            return
        if call_id not in orch.calls:
            await ws.close(code=4404)
            return
        await pump(ws, f"call:{call_id}")

    @app.websocket("/ws/wall")
    async def ws_wall(ws: WebSocket, token: str | None = Query(default=None)):
        if await ws_guard(ws, token):
            await pump(ws, "wall")

    @app.websocket("/ws/audio/{call_id}")
    async def ws_audio(ws: WebSocket, call_id: str, speaker: str = "caller", token: str | None = Query(default=None)):
        """16 kHz mono PCM16 frames in; 3 s windows are transcribed and ingested as final segments."""
        if not await ws_guard(ws, token):
            return
        if asr_backend is None or call_id not in orch.calls:
            await ws.close(code=4501, reason="no ASR backend configured (install faster-whisper and pass asr_backend)")
            return
        buf, window = bytearray(), 16000 * 2 * 3
        try:
            while True:
                buf += await ws.receive_bytes()
                if len(buf) >= window:
                    pcm, buf = bytes(buf), bytearray()
                    text, conf = await asr_backend.transcribe(pcm, 16000, orch.location.gaz.hotwords())
                    if text:
                        orch.ingest(Segment(call_id=call_id, speaker=speaker, text=text, final=True, confidence=conf))
        except WebSocketDisconnect:
            pass

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/", StaticFiles(directory=WEB), name="web")
    return app


app = create_app()
