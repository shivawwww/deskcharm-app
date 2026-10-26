"""Immutable, signed audit log (feature 47).

Append-only JSONL. Every record carries the previous record's HMAC, so deleting, editing
or reordering any line breaks verification. Key comes from COPILOT_AUDIT_KEY (HSM/Vault
in production).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path, key: bytes | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key = key or os.environ.get("COPILOT_AUDIT_KEY", "dev-only-audit-key").encode()
        self._lock = threading.Lock()
        self._seq, self._prev = 0, "0" * 64
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._seq, self._prev = rec["seq"], rec["mac"]

    def _mac(self, body: dict) -> str:
        return hmac.new(self.key, json.dumps(body, sort_keys=True, ensure_ascii=False).encode(), hashlib.sha256).hexdigest()

    def write(self, call_id: str | None, actor: str, action: str, data: dict | None = None) -> dict:
        with self._lock:
            self._seq += 1
            body = {"seq": self._seq, "ts": round(time.time(), 3), "call_id": call_id, "actor": actor,
                    "action": action, "data": data or {}, "prev": self._prev}
            rec = {**body, "mac": self._mac(body)}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._prev = rec["mac"]
            return rec

    def read(self, call_id: str | None = None, limit: int = 500) -> list[dict]:
        if not self.path.exists():
            return []
        recs = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if call_id:
            recs = [r for r in recs if r["call_id"] == call_id]
        return recs[-limit:]

    def verify(self) -> dict:
        prev, n = "0" * 64, 0
        if not self.path.exists():
            return {"ok": True, "records": 0}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            mac = rec.pop("mac")
            if rec["prev"] != prev or not hmac.compare_digest(mac, self._mac(rec)):
                return {"ok": False, "records": n, "broken_at_seq": rec["seq"]}
            prev, n = mac, n + 1
        return {"ok": True, "records": n}
