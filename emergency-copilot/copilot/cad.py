"""CAD adapter layer (feature 28). One interface, one adapter per vendor."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol


class CADAdapter(Protocol):
    def update_field(self, call_id: str, key: str, value: Any, meta: dict) -> None: ...
    def close_incident(self, call_id: str) -> None: ...


class MockCAD:
    """Stores the incident record as JSON files under var/cad/ so the full loop can be demonstrated."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.records: dict[str, dict] = {}

    def update_field(self, call_id: str, key: str, value: Any, meta: dict) -> None:
        rec = self.records.setdefault(call_id, {"call_id": call_id, "fields": {}, "closed": False})
        rec["fields"][key] = value
        self._flush(call_id)

    def close_incident(self, call_id: str) -> None:
        self.records.setdefault(call_id, {"call_id": call_id, "fields": {}})["closed"] = True
        self._flush(call_id)

    def _flush(self, call_id: str) -> None:
        (self.root / f"{call_id}.json").write_text(json.dumps(self.records[call_id], indent=2, ensure_ascii=False), encoding="utf-8")


class RestCAD:
    """Generic REST adapter. Map to the vendor's real payloads in the integration toolkit."""

    def __init__(self, base_url: str, token: str | None = None):
        self.base, self.token = base_url.rstrip("/"), token or os.environ.get("CAD_TOKEN")

    def _post(self, path: str, body: dict) -> None:
        import httpx

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        httpx.post(f"{self.base}{path}", json=body, headers=headers, timeout=3.0).raise_for_status()

    def update_field(self, call_id: str, key: str, value: Any, meta: dict) -> None:
        self._post(f"/incidents/{call_id}/fields", {"key": key, "value": value, "meta": meta})

    def close_incident(self, call_id: str) -> None:
        self._post(f"/incidents/{call_id}/close", {})
