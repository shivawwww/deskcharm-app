"""Speech ingestion (features 1-6). The orchestrator only ever sees `Segment`s, so any ASR plugs in here.

  * ScriptedASR       replays a scenario as word-by-word partials then a final: demo, tests, latency benchmarks
  * FasterWhisperBackend  optional real ASR for audio frames (needs `pip install faster-whisper`; not exercised in CI)
  * Browser / external ASR can simply POST segments to /api/calls/{id}/segments
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator, Iterator, Protocol

from .models import Segment

SCENARIOS = Path(__file__).parent / "data" / "scenarios"


def load_scenarios() -> dict[str, dict]:
    return {sc["id"]: sc for sc in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCENARIOS.glob("*.json")))}


def segments_for_turn(call_id: str, turn: dict, words_per_partial: int = 2) -> Iterator[Segment]:
    """Yield streaming partials and the final segment for one scripted turn."""
    words = turn["text"].split()
    conf = turn.get("confidence", 0.92)
    for n in range(words_per_partial, len(words), words_per_partial):
        yield Segment(call_id=call_id, speaker=turn["speaker"], text=" ".join(words[:n]), final=False, confidence=conf)
    yield Segment(call_id=call_id, speaker=turn["speaker"], text=turn["text"], final=True, confidence=conf)


class ScriptedASR:
    def __init__(self, scenario: dict, speed: float = 3.0, partial_interval_s: float = 0.25):
        self.scenario, self.speed, self.interval = scenario, max(0.1, speed), partial_interval_s

    async def stream(self, call_id: str) -> AsyncIterator[Segment]:
        for turn in self.scenario["turns"]:
            for seg in segments_for_turn(call_id, turn):
                yield seg
                await asyncio.sleep(self.interval / self.speed)
            await asyncio.sleep(0.6 / self.speed)


class ASRBackend(Protocol):
    async def transcribe(self, pcm16: bytes, sample_rate: int, hotwords: list[str]) -> tuple[str, float]: ...


class FasterWhisperBackend:
    """Real-ASR adapter. Biases decoding with the gazetteer vocabulary (contextual biasing, feature 6)."""

    def __init__(self, model: str = "large-v3", device: str = "auto", compute_type: str = "int8"):
        from faster_whisper import WhisperModel  # lazy: optional dependency

        self.model = WhisperModel(model, device=device, compute_type=compute_type)

    async def transcribe(self, pcm16: bytes, sample_rate: int, hotwords: list[str]) -> tuple[str, float]:
        import numpy as np

        audio = np.frombuffer(pcm16, dtype=np.int16).astype("float32") / 32768.0
        if sample_rate != 16000:
            raise ValueError("expected 16 kHz mono PCM16")

        def run() -> tuple[str, float]:
            segs, _ = self.model.transcribe(audio, beam_size=1, vad_filter=True, hotwords=" ".join(hotwords[:60]) or None)
            segs = list(segs)
            text = " ".join(s.text.strip() for s in segs).strip()
            conf = sum(2.718281828 ** s.avg_logprob for s in segs) / len(segs) if segs else 0.0
            return text, round(min(1.0, conf), 2)

        return await asyncio.to_thread(run)

    async def transcribe_file(self, path: str, language: str | None, hotwords: list[str]) -> tuple[str, float, str]:
        """Transcribe an audio file (wav, mp3, ...). Returns (text, confidence, detected language)."""
        def run() -> tuple[str, float, str]:
            segs, info = self.model.transcribe(path, beam_size=1, vad_filter=True, language=language, hotwords=" ".join(hotwords[:60]) or None)
            segs = list(segs)
            conf = sum(2.718281828 ** s.avg_logprob for s in segs) / len(segs) if segs else 0.0
            return " ".join(s.text.strip() for s in segs).strip(), round(min(1.0, conf), 2), info.language

        return await asyncio.to_thread(run)
