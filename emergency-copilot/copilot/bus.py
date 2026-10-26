"""In-process event bus with per-topic ordering and replay (swap for Kafka / NATS JetStream in production)."""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Any


class EventBus:
    def __init__(self, replay: int = 100):
        self._buf: dict[str, deque] = defaultdict(lambda: deque(maxlen=replay))
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._seq: dict[str, int] = defaultdict(int)

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        self._seq[topic] += 1
        evt = {**event, "_seq": self._seq[topic]}
        self._buf[topic].append(evt)
        for q in list(self._subs[topic]):
            q.put_nowait(evt)

    def replay(self, topic: str, last: int = 1) -> list[dict]:
        return list(self._buf[topic])[-last:]

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs[topic].add(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        self._subs[topic].discard(q)
