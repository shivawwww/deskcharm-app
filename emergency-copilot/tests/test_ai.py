"""AI integrations are tested with fake providers: no network, no keys."""
import json
import sys
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from copilot.agent import SupportAgent
from copilot.aisummary import draft_summary
from copilot.api import create_app
from copilot.helpdocs import HelpIndex
from copilot.llm import AnthropicLLM, LLMConfig, LLMError, build_llm
from copilot.models import Segment


def text(t):
    return NS(type="text", text=t)


def tool_use(i, name, **inp):
    return NS(type="tool_use", id=i, name=name, input=inp)


class FakeLLM:
    """Scripted responses; records every request so tests can inspect what would be sent."""

    supports_tools = True

    def __init__(self, script):
        self.script, self.requests = list(script), []

    def create(self, system, messages, tools=None):
        self.requests.append({"system": system, "messages": json.loads(json.dumps(messages, default=lambda o: o.__dict__)), "tools": [t["name"] for t in tools or []]})
        return self.script.pop(0)

    def complete_text(self, system, user):
        self.requests.append({"system": system, "user": user})
        return self.script.pop(0)


def resp(stop, *blocks):
    return NS(stop_reason=stop, content=list(blocks))


def agent_for(orch, script, send=False):
    cfg = LLMConfig(provider="anthropic", send_call_data=send)
    return SupportAgent(orch, FakeLLM(script), cfg), cfg


def test_help_search_is_relevant():
    h = HelpIndex()
    assert "shadow" in h.search("what is shadow mode")[0]["q"].lower()
    assert h.search("qwertyuiop") == []


def test_agent_tool_loop_and_audit(orch):
    ag, _ = agent_for(orch, [resp("tool_use", tool_use("t1", "get_sop_questions", incident_type="MEDICAL")),
                             resp("end_turn", text("Medical calls ask: age, breathing, main problem."))])
    out = ag.chat([{"role": "user", "content": "what does the medical protocol ask?"}])
    assert out["mode"] == "agent" and out["tools"][0]["name"] == "get_sop_questions" and "age" in out["reply"]
    second = ag.llm.requests[1]["messages"]
    assert second[-1]["role"] == "user" and second[-1]["content"][0]["type"] == "tool_result"  # all results in one user message
    rec = [r for r in orch.audit.read() if r["action"] == "agent_chat"][0]
    assert rec["data"]["tools"] == ["get_sop_questions"] and rec["data"]["call_data_shared"] is False


def test_call_tools_hidden_without_opt_in_and_read_only(orch):
    ag, _ = agent_for(orch, [resp("end_turn", text("ok"))], send=False)
    ag.chat([{"role": "user", "content": "hi"}])
    assert ag.llm.requests[0]["tools"] == ["search_help", "get_sop_questions", "get_system_status"]
    assert "never" in ag.llm.requests[0]["system"].lower() or "cannot" in ag.llm.requests[0]["system"].lower()
    # a hallucinated call tool is refused by policy even if the model asks for it
    assert "disabled by policy" in ag.run_tool("explain_call", {"call_id": "x"})
    ag2, _ = agent_for(orch, [resp("end_turn", text("ok"))], send=True)
    ag2.chat([{"role": "user", "content": "hi"}])
    assert {"list_calls", "explain_call"} <= set(ag2.llm.requests[0]["tools"])
    assert not {t["name"] for t in ag2.tools()} & {"dispatch", "accept_field", "set_feature"}


def test_explain_call_exposes_reasons_not_raw_transcript(orch):
    orch.start_call("A1")
    orch.ingest(Segment(call_id="A1", text="my father is not breathing at Hamdan Street", final=True))
    ag, _ = agent_for(orch, [], send=True)
    out = ag.run_tool("explain_call", {"call_id": "A1"})
    assert out["priority"] and any("Not breathing" in r for r in out["reasons"]) and "transcript" not in out
    assert ag.run_tool("explain_call", {"call_id": "nope"}) == "Unknown call id."


def test_refusal_and_provider_errors_degrade_to_help(orch):
    ag, _ = agent_for(orch, [resp("refusal")])
    assert "declined" in ag.chat([{"role": "user", "content": "x"}])["reply"]

    class Boom(FakeLLM):
        def create(self, *a, **k):
            raise ConnectionError("down")

    ag = SupportAgent(orch, Boom([]), LLMConfig(provider="anthropic"))
    out = ag.chat([{"role": "user", "content": "what is shadow mode"}])
    assert out["mode"] == "faq" and "shadow" in out["reply"].lower()


def test_faq_mode_without_llm_and_rate_limit(orch):
    ag = SupportAgent(orch, None, LLMConfig(), max_requests_per_min=2)
    assert ag.status()["faq_only"] is True and ag.status()["read_only"] is True
    assert "shadow" in ag.chat([{"role": "user", "content": "what is shadow mode"}])["reply"].lower()
    ag.chat([{"role": "user", "content": "kill switch"}])
    assert ag.chat([{"role": "user", "content": "again"}])["mode"] == "rate_limited"
    with pytest.raises(ValueError):
        SupportAgent(orch, None, LLMConfig()).chat([{"role": "assistant", "content": "x"}])


def test_draft_summary_needs_opt_in_and_is_audited(orch):
    orch.start_call("S1")
    orch.ingest(Segment(call_id="S1", text="fire at Hamdan Street, two people trapped", final=True))
    llm = FakeLLM(["A fire is reported at Hamdan Street with two people trapped."])
    with pytest.raises(LLMError):
        draft_summary(orch, llm, LLMConfig(provider="anthropic", send_call_data=False), "S1")
    assert llm.requests == []  # nothing was sent
    sug = draft_summary(orch, llm, LLMConfig(provider="anthropic", send_call_data=True), "S1")
    assert orch.calls["S1"].fields["narrative_ai"].meta["source"] == "llm" and sug.status == "suggested"
    assert "untrusted" in llm.requests[0]["system"] and "Hamdan Street" in llm.requests[0]["user"]
    assert any(r["action"] == "llm_request" and r["data"]["chars_sent"] > 0 for r in orch.audit.read("S1"))
    assert orch.audit.verify()["ok"]


def test_build_llm_off_by_default_and_reports_reason(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert build_llm(LLMConfig(provider="off")) is None
    cfg = LLMConfig(provider="anthropic")
    assert build_llm(cfg) is None and ("not installed" in cfg.reason or "ANTHROPIC_API_KEY" in cfg.reason)  # CI has no anthropic package
    monkeypatch.setitem(sys.modules, "anthropic", NS())  # package present, key missing
    cfg = LLMConfig(provider="anthropic")
    assert build_llm(cfg) is None and "ANTHROPIC_API_KEY" in cfg.reason
    cfg = LLMConfig(provider="openai_compat")
    assert build_llm(cfg) is None and "BASE_URL" in cfg.reason
    assert LLMConfig.from_env().send_call_data is False


def test_anthropic_wrapper_sends_expected_request():
    calls = {}

    class Msgs:
        def create(self, **kw):
            calls.update(kw)
            return resp("end_turn", text("hi"))

    llm = AnthropicLLM(LLMConfig(provider="anthropic"), client=NS(beta=NS(messages=Msgs())))
    assert llm.complete_text("sys", "q") == "hi"
    assert calls["model"] == "claude-opus-5-5" and calls["betas"] == ["server-side-fallback-2026-07-01"] and calls["fallbacks"] == "default"
    assert "temperature" not in calls and "thinking" not in calls


@pytest.fixture
def ai_client(tmp_path):
    llm = FakeLLM([resp("end_turn", text("Shadow mode hides AI output.")), "Draft narrative."])
    app = create_app(tmp_path, llm=llm, llm_cfg=LLMConfig(provider="anthropic", send_call_data=True))
    with TestClient(app) as c:
        yield c


def test_agent_and_summary_endpoints(ai_client):
    assert ai_client.get("/api/agent/status").json()["mode"] == "agent"
    r = ai_client.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "what is shadow mode?"}]})
    assert r.json()["reply"] == "Shadow mode hides AI output."
    assert ai_client.post("/api/agent/chat", json={"messages": []}).status_code == 422
    cid = ai_client.post("/api/calls", json={}).json()["call_id"]
    ai_client.post(f"/api/calls/{cid}/segments", json={"text": "fire at Hamdan Street", "final": True})
    assert ai_client.post(f"/api/calls/{cid}/ai-summary").json()["label"] == "Narrative (AI draft)"


def test_endpoints_without_ai_or_asr(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        assert c.get("/api/agent/status").json()["faq_only"] is True
        assert "shadow" in c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "shadow mode"}]}).json()["reply"].lower()
        cid = c.post("/api/calls", json={}).json()["call_id"]
        assert c.post(f"/api/calls/{cid}/ai-summary").status_code == 409
        assert c.post("/api/transcribe", content=b"RIFF").status_code == 501


def test_transcribe_with_fake_local_asr(tmp_path):
    class FakeASR:
        async def transcribe_file(self, path, language, hotwords):
            return "مرحبا", 0.9, language or "ar"

    with TestClient(create_app(tmp_path, asr_backend=FakeASR())) as c:
        out = c.post("/api/transcribe?language=ar", content=b"audio-bytes").json()
        assert out == {"text": "مرحبا", "confidence": 0.9, "language": "ar"}
        assert c.post("/api/transcribe", content=b"").status_code == 413


# ------------------------------------------------------------------ local LLM (Ollama / OpenAI-compatible)
import httpx  # noqa: E402

from copilot.llm import OpenAICompatLLM, ToolsUnsupported, _to_openai  # noqa: E402


def local_llm(handler, **extra):
    cfg = LLMConfig(provider="ollama", model="llama3.1", base_url="http://localhost:11434/v1", extra={"tools": True, **extra})
    return OpenAICompatLLM(cfg, http=httpx.Client(transport=httpx.MockTransport(handler))), cfg


def oa(message, finish="stop"):
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}]})


def test_ollama_preset_from_env(monkeypatch):
    monkeypatch.setenv("COPILOT_LLM", "ollama")
    for k in ("COPILOT_LLM_MODEL", "COPILOT_LLM_BASE_URL", "COPILOT_LLM_TOOLS"):
        monkeypatch.delenv(k, raising=False)
    cfg = LLMConfig.from_env()
    assert (cfg.provider, cfg.model, cfg.base_url, cfg.is_local) == ("ollama", "llama3.1", "http://localhost:11434/v1", True)
    monkeypatch.setenv("COPILOT_LLM_TOOLS", "0")
    assert LLMConfig.from_env().extra["tools"] is False
    monkeypatch.setenv("COPILOT_LLM", "openai_compat")
    monkeypatch.setenv("COPILOT_LLM_BASE_URL", "https://llm.example.com/v1")
    assert LLMConfig.from_env().is_local is False


def test_history_conversion_to_openai_format():
    blocks = [NS(type="text", text="checking"), NS(type="tool_use", id="c1", name="get_sop_questions", input={"incident_type": "FIRE"})]
    msgs = [{"role": "user", "content": "q"}, {"role": "assistant", "content": blocks},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "[...]"}]}]
    out = _to_openai("SYS", msgs)
    assert out[0] == {"role": "system", "content": "SYS"} and out[2]["tool_calls"][0]["function"]["name"] == "get_sop_questions"
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"incident_type": "FIRE"}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "[...]"}


def test_local_agent_tool_loop(orch):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        if len(seen) == 1:
            return oa({"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_sop_questions", "arguments": '{"incident_type": "FIRE"}'}}]}, "tool_calls")
        return oa({"content": "Fire calls ask whether anyone is trapped."})

    llm, cfg = local_llm(handler)
    out = SupportAgent(orch, llm, cfg).chat([{"role": "user", "content": "what does the fire protocol ask?"}])
    assert out["mode"] == "agent" and "trapped" in out["reply"] and out["tools"][0]["name"] == "get_sop_questions"
    assert seen[0]["tools"][0]["function"]["name"] == "search_help" and seen[1]["messages"][-1]["role"] == "tool"
    assert all(t["function"]["name"] not in ("explain_call", "list_calls") for t in seen[0]["tools"])  # call tools stay hidden without opt-in


def test_local_model_without_tool_support_falls_back_to_help_assistant(orch):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append("tools" in body)
        return httpx.Response(400, json={"error": "does not support tools"}) if "tools" in body else oa({"content": "Shadow mode hides AI output."})

    llm, cfg = local_llm(handler)
    ag = SupportAgent(orch, llm, cfg)
    out = ag.chat([{"role": "user", "content": "what is shadow mode?"}])
    assert out["mode"] == "assistant" and "Shadow" in out["reply"] and calls == [True, False] and llm.supports_tools is False
    assert ag.chat([{"role": "user", "content": "again"}])["mode"] == "assistant"  # remembered, no wasted 400


def test_local_status_ping_and_unreachable_degrades(orch):
    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        raise httpx.ConnectError("refused")

    llm, cfg = local_llm(handler)
    ag = SupportAgent(orch, llm, cfg)
    st = ag.status()
    assert st["local"] is True and st["reachable"] is True and st["provider"] == "ollama"
    out = ag.chat([{"role": "user", "content": "what is shadow mode"}])
    assert out["mode"] == "faq" and "unavailable" in out["reply"]  # server down: keyword help, never an error screen
