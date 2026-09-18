"""追加式领域事件。

事件一经写入不可修改、不可删除；任何更正都通过新的“更正事件”追加表达。
事件按写入顺序获得全局递增 seq；业务时间（如事故发生时间）存放在 payload 中，
断网补传或服务恢复后的“按事故发生顺序整理”由投影层依据业务时间完成。
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_instant(value: str | datetime) -> datetime:
    """解析带时区的 ISO8601 时间并归一化为 UTC，保证跨时区报案可比较。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        # 不接受无时区时间：跨时区场景下裸时间没有语义
        raise ValueError(f"时间必须带时区偏移: {value!r}")
    return dt.astimezone(timezone.utc)


def instant_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class Event:
    seq: int
    stream_id: str          # 通常为 incident_id；保单级事件用 policy_id
    type: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurred_at: datetime = field(default_factory=utcnow)   # 入库时间（墙钟）
    client_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "seq": self.seq,
            "stream_id": self.stream_id,
            "type": self.type,
            "payload": self.payload,
            "occurred_at": instant_str(self.occurred_at),
            "client_event_id": self.client_event_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        return cls(
            event_id=raw["event_id"],
            seq=raw["seq"],
            stream_id=raw["stream_id"],
            type=raw["type"],
            payload=raw["payload"],
            occurred_at=parse_instant(raw["occurred_at"]),
            client_event_id=raw.get("client_event_id"),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
