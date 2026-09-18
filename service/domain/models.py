"""保单与事故的状态模型（由事件流投影得到，可随时重放重建）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from .events import parse_instant


class IncidentStatus(str, Enum):
    REGISTERED = "REGISTERED"                    # 已登记，等待范围研判
    DUPLICATE = "DUPLICATE"                      # 重复通知，已并入主事故
    PENDING_SCOPE = "PENDING_SCOPE"              # 等待保单范围/通知时限判定
    OUT_OF_SCOPE = "OUT_OF_SCOPE"                # 超出保单范围，赔付冻结
    UNDER_REVIEW = "UNDER_REVIEW"                # 范围成立，调查审单据中
    NOTICE_LATE = "NOTICE_LATE"                  # 超通知时限（可解释，需核保人裁量）
    PARTIAL_LOSS = "PARTIAL_LOSS"                # 部分损失已申报
    ADJUSTED = "ADJUSTED"                        # 损失已定损
    ADVANCE_AUTHORIZED = "ADVANCE_AUTHORIZED"    # 紧急预付已授权
    PAYMENT_PENDING = "PAYMENT_PENDING"          # 赔付指令已发起
    PAID = "PAID"                                # 定损金额赔付完成
    SUBROGATION = "SUBROGATION"                  # 追偿中
    RECOVERED = "RECOVERED"                      # 已到追偿款（含迟到回执重开）
    CLOSED = "CLOSED"


@dataclass
class Endorsement:
    endorsement_id: str
    effective_from: object      # datetime(UTC)
    coverages_added: list[str]
    regions_added: list[str]
    note: str = ""


@dataclass
class Policy:
    policy_id: str
    coverages: set[str] = field(default_factory=set)
    regions: set[str] = field(default_factory=set)
    deductibles: dict[str, Decimal] = field(default_factory=dict)
    notice_limits_hours: dict[str, int] = field(default_factory=dict)
    currency: str = "USD"
    endorsements: list[Endorsement] = field(default_factory=list)

    def coverages_at(self, occurred_at) -> set[str]:
        """事故发生时有效（含已生效批改）的承保险种。"""
        result = set(self.coverages)
        for e in self.endorsements:
            if e.effective_from <= occurred_at:
                result.update(e.coverages_added)
        return result

    def regions_at(self, occurred_at) -> set[str]:
        result = set(self.regions)
        for e in self.endorsements:
            if e.effective_from <= occurred_at:
                result.update(e.regions_added)
        return result

    def deductible_for(self, loss_type: str) -> Decimal:
        return self.deductibles.get(loss_type, Decimal("0"))

    def notice_limit_hours(self, loss_type: str) -> int | None:
        return self.notice_limits_hours.get(loss_type)


@dataclass
class Material:
    material_id: str
    kind: str
    in_scope: bool
    reason: str
    received_at: object
    summary: str                        # 原始摘要，永不替换
    corrections: list[dict] = field(default_factory=list)


@dataclass
class Advance:
    amount: Decimal
    cap: Decimal
    authorizer: str
    authorized_at: object


@dataclass
class Incident:
    incident_id: str
    policy_id: str
    loss_type: str = ""
    region: str = ""
    occurred_at: object = None
    notified_at: object = None
    reporter: str = ""
    fingerprint: str = ""
    status: IncidentStatus = IncidentStatus.REGISTERED
    status_reason: str = ""
    duplicate_of: str | None = None
    owner: str | None = None
    in_scope: bool | None = None
    scope_basis: str = ""
    within_notice: bool | None = None
    notice_deadline: object = None
    claimed_amount: Decimal | None = None
    adjusted_amount: Decimal | None = None
    deductible_applied: Decimal | None = None
    materials: list[Material] = field(default_factory=list)
    advances: list[Advance] = field(default_factory=list)
    payments: list[dict] = field(default_factory=list)
    acknowledgements: list[dict] = field(default_factory=list)
    investigation: list[dict] = field(default_factory=list)
    subrogation: dict | None = None
    recoveries: list[dict] = field(default_factory=list)
    corrections: list[dict] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)

    @property
    def paid_total(self) -> Decimal:
        return sum((Decimal(p["amount"]) for p in self.payments), Decimal("0"))

    @property
    def recovered_total(self) -> Decimal:
        return sum((Decimal(r["amount"]) for r in self.recoveries), Decimal("0"))

    @property
    def has_blocking_material(self) -> bool:
        return any(not m.in_scope for m in self.materials)
