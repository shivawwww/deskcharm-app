"""SOP engine (features 22, 23): deterministic protocol logic. AI proposes, rules decide the next question."""
from __future__ import annotations

import json
from pathlib import Path

from .models import ChecklistItem

DATA = Path(__file__).parent / "data"


class SopEngine:
    def __init__(self, path: Path | None = None):
        cfg = json.loads((path or DATA / "sop.json").read_text(encoding="utf-8"))
        self.generic = cfg["generic"]
        self.types = cfg["types"]
        self.instructions = cfg["instructions"]

    def checklist(self, incident_type: str, filled: set[str]) -> list[ChecklistItem]:
        items = self.generic + self.types.get(incident_type, [])
        return [ChecklistItem(id=i["id"], slot=i["slot"], question=i["q"], done=i["slot"] in filled) for i in items]

    @staticmethod
    def next_question(checklist: list[ChecklistItem], lang: str = "en") -> dict[str, str] | None:
        for it in checklist:
            if not it.done:
                return {"id": it.id, "en": it.question["en"], "ar": it.question["ar"], "primary": "ar" if lang in ("ar", "mixed") else "en"}
        return None

    def instructions_for(self, cue_ids: set[str]) -> list[dict[str, str]]:
        return [{"cue": i["cue"], "en": i["text"]["en"], "ar": i["text"]["ar"], "note": "Sample text, replace with approved protocol"}
                for i in self.instructions if i["cue"] in cue_ids]
