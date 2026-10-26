"""Optional LLM providers. OFF by default: nothing is sent anywhere unless the operator configures it.

    COPILOT_LLM=off | anthropic | ollama | openai_compat
    COPILOT_LLM_MODEL            default claude-opus-5-5 (anthropic)
    COPILOT_LLM_SEND_CALL_DATA=1 separate opt-in before any call transcript / case data may be sent to the LLM
    ANTHROPIC_API_KEY            anthropic provider (read by the SDK)
    ollama                       LOCAL model through Ollama (default http://localhost:11434/v1, default model llama3.1): data stays on this machine
    COPILOT_LLM_BASE_URL / COPILOT_LLM_API_KEY / COPILOT_LLM_MODEL   openai_compat provider (any OpenAI-compatible server,
                                 for example a self-hosted vLLM or llama.cpp server, so data can stay on premise)
    COPILOT_LLM_TOOLS=0          disable tool use for local models that do not support it (agent then answers from the help base)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

DEFAULT_MODEL = "claude-opus-5-5"


class LLMError(Exception):
    pass


class ToolsUnsupported(LLMError):
    """The model or server rejected tool use; the caller should fall back to a plain assistant."""


OLLAMA_URL = "http://localhost:11434/v1"
OLLAMA_MODEL = "llama3.1"


@dataclass
class LLMConfig:
    provider: str = "off"
    model: str = DEFAULT_MODEL
    send_call_data: bool = False
    base_url: str = ""
    max_tokens: int = 4096
    reason: str = ""  # why a requested provider is unavailable
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "LLMConfig":
        prov = os.environ.get("COPILOT_LLM", "off").lower()
        default_model = OLLAMA_MODEL if prov == "ollama" else DEFAULT_MODEL
        cfg = cls(provider=prov if prov in ("anthropic", "openai_compat", "ollama") else "off",
                  model=os.environ.get("COPILOT_LLM_MODEL", default_model),
                  send_call_data=os.environ.get("COPILOT_LLM_SEND_CALL_DATA") == "1",
                  base_url=os.environ.get("COPILOT_LLM_BASE_URL", OLLAMA_URL if prov == "ollama" else "").rstrip("/"))
        cfg.extra["tools"] = os.environ.get("COPILOT_LLM_TOOLS", "1") != "0"
        if prov not in ("off", "anthropic", "openai_compat", "ollama"):
            cfg.reason = f"unknown COPILOT_LLM value '{prov}'"
        return cfg

    @property
    def is_local(self) -> bool:
        host = urlparse(self.base_url).hostname or ""
        return self.provider == "ollama" or host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")


class AnthropicLLM:
    """Claude via the official SDK. Supports tool use (needed by the support agent)."""

    supports_tools = True

    def __init__(self, cfg: LLMConfig, client: Any = None):
        self.cfg = cfg
        if client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover
                raise LLMError("the 'anthropic' package is not installed (pip install anthropic)") from e
            if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                raise LLMError("ANTHROPIC_API_KEY is not set")
            client = anthropic.Anthropic()
        self.client = client

    def create(self, system: str, messages: list[dict], tools: list[dict] | None = None) -> Any:
        kwargs: dict[str, Any] = dict(model=self.cfg.model, max_tokens=self.cfg.max_tokens, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        # server-side refusal fallback (default-on for this model family); beta endpoint, same response shape
        return self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)

    def complete_text(self, system: str, user: str) -> str:
        resp = self.create(system, [{"role": "user", "content": user}])
        if resp.stop_reason == "refusal":
            raise LLMError("the model declined this request")
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


@dataclass
class _Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict | None = None


@dataclass
class _Resp:
    stop_reason: str
    content: list


def _to_openai(system: str, messages: list[dict]) -> list[dict]:
    """Anthropic-style history (text, tool_use blocks, tool_result dicts) -> OpenAI chat messages."""
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
        elif m["role"] == "assistant":
            calls = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.input or {})}} for b in c if b.type == "tool_use"]
            d: dict[str, Any] = {"role": "assistant", "content": "".join(b.text for b in c if b.type == "text") or None}
            if calls:
                d["tool_calls"] = calls
            out.append(d)
        else:
            out.extend({"role": "tool", "tool_call_id": r["tool_use_id"], "content": r["content"]} for r in c)
    return out


class OpenAICompatLLM:
    """Any OpenAI-compatible chat endpoint: Ollama, llama.cpp server, vLLM. Supports tool calling when the model does."""

    def __init__(self, cfg: LLMConfig, http: Any = None):
        if not cfg.base_url:
            raise LLMError("COPILOT_LLM_BASE_URL is not set")
        import httpx

        self.cfg = cfg
        self._http = http or httpx.Client(timeout=60.0)
        self.supports_tools = bool(cfg.extra.get("tools", True))

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {os.environ['COPILOT_LLM_API_KEY']}"} if os.environ.get("COPILOT_LLM_API_KEY") else {}

    def ping(self) -> bool:
        try:
            return self._http.get(f"{self.cfg.base_url}/models", headers=self._headers(), timeout=2.0).status_code < 400
        except Exception:
            return False

    def create(self, system: str, messages: list[dict], tools: list[dict] | None = None) -> _Resp:
        body: dict[str, Any] = {"model": self.cfg.model, "max_tokens": self.cfg.max_tokens, "messages": _to_openai(system, messages)}
        if tools and self.supports_tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
        r = self._http.post(f"{self.cfg.base_url}/chat/completions", headers=self._headers(), json=body)
        if r.status_code == 400 and "tools" in body:
            self.supports_tools = False  # remember: this model/server cannot do tool calls
            raise ToolsUnsupported("the local model does not support tool use")
        if r.status_code >= 400:
            raise LLMError(f"provider returned HTTP {r.status_code}")
        choice = r.json()["choices"][0]
        msg, blocks = choice["message"], []
        if msg.get("content"):
            blocks.append(_Block("text", text=msg["content"]))
        for call in msg.get("tool_calls") or []:
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            blocks.append(_Block("tool_use", id=call.get("id") or call["function"]["name"], name=call["function"]["name"], input=args if isinstance(args, dict) else {}))
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else ("max_tokens" if choice.get("finish_reason") == "length" else "end_turn")
        return _Resp(stop, blocks)

    def complete_text(self, system: str, user: str) -> str:
        resp = self.create(system, [{"role": "user", "content": user}])
        return "".join(b.text for b in resp.content if b.type == "text").strip()


def build_llm(cfg: LLMConfig) -> AnthropicLLM | OpenAICompatLLM | None:
    """Return a provider, or None (and set cfg.reason) so the app keeps working without AI."""
    if cfg.provider == "off":
        return None
    try:
        return AnthropicLLM(cfg) if cfg.provider == "anthropic" else OpenAICompatLLM(cfg)
    except LLMError as e:
        cfg.reason = str(e)
        return None
