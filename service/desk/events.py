"""追加式事件存储。

规则：
- 事件只追加、不改写；任何状态更正都是一条新事件；
- 每条事件带业务时间（事故/回执/节点实际发生时间）与到达序号；
- 处置链按业务时间排序、同一时刻按到达序号排序，
  因此断网补传与系统恢复后仍按事故发生顺序整理。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value: str) -> datetime:
    """解析 ISO 时间戳；必须带时区，否则跨时区排序无从谈起。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间戳必须包含时区偏移: {value!r}")
    return dt


@dataclass
class Event:
    seq: int               # 到达顺序（存储分配，单调递增）
    event_id: str
    type: str
    stream: str            # 案件 id 或 policy:{policy_id}
    business_time: str     # 业务发生时间（ISO，带时区）
    recorded_at: str       # 服务落账时间（UTC）
    actor: str             # 操作人（理赔员/承保人门户/系统）
    payload: dict

    def sort_key(self):
        return (parse_ts(self.business_time).astimezone(timezone.utc), self.seq)


class EventStore:
    """内存事件存储，可选 JSONL 落盘以支持系统恢复。"""

    def __init__(self, path: str | None = None):
        self._events: list[Event] = []
        self._lock = threading.Lock()
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._events.append(Event(**json.loads(line)))
            self._events.sort(key=lambda e: e.seq)

    def append(self, type: str, stream: str, business_time: str,
               actor: str, payload: dict) -> Event:
        with self._lock:
            event = Event(
                seq=len(self._events) + 1,
                event_id=uuid.uuid4().hex,
                type=type,
                stream=stream,
                business_time=business_time,
                recorded_at=utcnow_iso(),
                actor=actor,
                payload=payload,
            )
            self._events.append(event)
            if self._path:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
            return event

    def events(self, stream: str | None = None) -> list[Event]:
        """按到达顺序返回事件。"""
        return [e for e in self._events if stream is None or e.stream == stream]

    def chain(self, stream: str) -> list[Event]:
        """处置链：按业务时间（事故发生顺序）整理，同一时刻按到达顺序。"""
        return sorted(self.events(stream), key=lambda e: e.sort_key())
