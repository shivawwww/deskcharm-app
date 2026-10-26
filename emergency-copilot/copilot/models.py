from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

Speaker = Literal["caller", "calltaker", "third_party"]
FieldStatus = Literal["suggested", "accepted", "rejected", "edited"]


class Segment(BaseModel):
    call_id: str
    speaker: Speaker = "caller"
    text: str
    final: bool = False
    confidence: float = 1.0
    seq: int = 0
    lang: str | None = None
    dialect: str | None = None
    code_switched: bool = False
    ts: float = Field(default_factory=time.time)


class FieldSuggestion(BaseModel):
    key: str
    label: str
    value: Any
    confidence: float
    evidence: list[str] = []
    explanation: str = ""
    meta: dict[str, Any] = {}
    status: FieldStatus = "suggested"
    updated_at: float = Field(default_factory=time.time)


class Cue(BaseModel):
    id: str
    label: str
    level: Literal["critical", "high"]
    evidence: str
    raised_at: float = Field(default_factory=time.time)
    acked: bool = False


class LocCandidate(BaseModel):
    name: str
    name_ar: str | None = None
    kind: str
    lat: float
    lon: float
    confidence: float
    region: str | None = None
    area: str | None = None
    source: Literal["speech", "aml"]
    evidence: str = ""
    note: str = ""


class SeverityReason(BaseModel):
    label: str
    points: int
    evidence: str = ""


class Severity(BaseModel):
    score: int = 0
    level: str = "P4"
    name: str = "Low"
    reasons: list[SeverityReason] = []


class ChecklistItem(BaseModel):
    id: str
    slot: str
    question: dict[str, str]
    done: bool = False


class CallState(BaseModel):
    call_id: str
    case_number: str = ""
    timeline: list[dict[str, Any]] = []
    status: Literal["active", "ended"] = "active"
    started_at: float = Field(default_factory=time.time)
    last_activity: float = Field(default_factory=time.time)
    ended_at: float | None = None
    caller_number: str | None = None
    aml: dict[str, float] | None = None
    scenario: str | None = None
    lang: str = "unknown"
    dialect: str | None = None
    code_switched: bool = False
    segments: list[Segment] = []
    partials: dict[str, Segment] = {}
    fields: dict[str, FieldSuggestion] = {}
    cues: dict[str, Cue] = {}
    candidates: list[LocCandidate] = []
    severity: Severity = Severity()
    summary: str = ""
    checklist: list[ChecklistItem] = []
    next_question: dict[str, str] | None = None
    instructions: list[dict[str, str]] = []
    timings_ms: dict[str, float] = {}
    seq: int = 0
