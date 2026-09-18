"""追加式事件存储。

- 内存实现加全局锁，seq 单调递增，写入即持久化（JSON 行文件，崩溃可重放恢复）。
- 客户端幂等键 client_event_id 用于断网补传：同一条报案重传不会产生第二起事故。
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from .events import Event, utcnow


class EventStore:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._client_ids: dict[str, int] = {}
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self._load()

    @contextmanager
    def transaction(self):
        """命令原子区：查重与追加在同一临界区内，并发报案不会穿透成两起事故。"""
        with self._lock:
            yield

    def _load(self) -> None:
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            evt = Event.from_dict(json.loads(line))
            self._events.append(evt)
            if evt.client_event_id:
                self._client_ids[evt.client_event_id] = evt.seq

    def _persist(self, evt: Event) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(evt.to_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def append(
        self,
        stream_id: str,
        type_: str,
        payload: dict,
        *,
        client_event_id: str | None = None,
    ) -> Event:
        with self._lock:
            if client_event_id and client_event_id in self._client_ids:
                # 断网补传：返回首次写入的事件，不重复登记
                return self._events[self._client_ids[client_event_id] - 1]
            seq = len(self._events) + 1
            evt = Event(
                seq=seq,
                stream_id=stream_id,
                type=type_,
                payload=payload,
                occurred_at=utcnow(),
                client_event_id=client_event_id,
            )
            self._events.append(evt)
            if client_event_id:
                self._client_ids[client_event_id] = seq
            self._persist(evt)
            return evt

    def all_events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def lookup_client_event_id(self, client_event_id: str) -> Event | None:
        with self._lock:
            seq = self._client_ids.get(client_event_id)
            return self._events[seq - 1] if seq else None

    def stream(self, stream_id: str) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.stream_id == stream_id]
