"""事件投影：从追加事件流重建保单/事故当前状态。

投影只做 fold，不做业务校验；所有规则在应用层写事件之前执行。
两个视图：
- by_seq：按入库顺序（处置链真实顺序）；
- by_occurrence：按事故发生时间（业务时间）排序，断网补传后重放即自动归位。
"""
from __future__ import annotations

from decimal import Decimal

from .events import Event, parse_instant
from .models import (
    Advance,
    Endorsement,
    Incident,
    IncidentStatus,
    Material,
    Policy,
)


class Projection:
    def __init__(self) -> None:
        self.policies: dict[str, Policy] = {}
        self.incidents: dict[str, Incident] = {}
        self.fingerprints: dict[str, str] = {}   # fingerprint -> 主事故 id

    # ---- 公共入口 ----------------------------------------------------------
    def apply(self, evt: Event) -> None:
        handler = getattr(self, f"_on_{evt.type.lower()}", None)
        if handler:
            handler(evt)

    def rebuild(self, events: list[Event]) -> None:
        self.__init__()
        for evt in sorted(events, key=lambda e: e.seq):
            self.apply(evt)

    def incidents_by_occurrence(self) -> list[Incident]:
        return sorted(
            self.incidents.values(),
            key=lambda i: (i.occurred_at is None, i.occurred_at, i.incident_id),
        )

    # ---- 保单事件 ----------------------------------------------------------
    def _on_policy_registered(self, e: Event) -> None:
        p = e.payload
        self.policies[e.stream_id] = Policy(
            policy_id=e.stream_id,
            coverages=set(p["coverages"]),
            regions=set(p["regions"]),
            deductibles={k: Decimal(str(v)) for k, v in p.get("deductibles", {}).items()},
            notice_limits_hours=p.get("notice_limits_hours", {}),
            currency=p.get("currency", "USD"),
        )

    def _on_endorsement_recorded(self, e: Event) -> None:
        policy = self.policies[e.stream_id]
        p = e.payload
        policy.endorsements.append(
            Endorsement(
                endorsement_id=p["endorsement_id"],
                effective_from=parse_instant(p["effective_from"]),
                coverages_added=list(p.get("coverages_added", [])),
                regions_added=list(p.get("regions_added", [])),
                note=p.get("note", ""),
            )
        )

    # ---- 事故事件 ----------------------------------------------------------
    def _incident(self, e: Event) -> Incident:
        return self.incidents[e.stream_id]

    def _on_incident_reported(self, e: Event) -> None:
        p = e.payload
        inc = Incident(
            incident_id=e.stream_id,
            policy_id=p["policy_id"],
            loss_type=p["loss_type"],
            region=p.get("region", ""),
            occurred_at=parse_instant(p["occurred_at"]),
            notified_at=parse_instant(p["notified_at"]),
            reporter=p.get("reporter", ""),
            fingerprint=p["fingerprint"],
            status=IncidentStatus(p.get("status", IncidentStatus.REGISTERED.value)),
            status_reason=p.get("status_reason", ""),
        )
        self.incidents[inc.incident_id] = inc
        self.fingerprints.setdefault(inc.fingerprint, inc.incident_id)

    def _on_duplicate_reported(self, e: Event) -> None:
        p = e.payload
        inc = Incident(
            incident_id=e.stream_id,
            policy_id=p["policy_id"],
            loss_type=p.get("loss_type", ""),
            region=p.get("region", ""),
            occurred_at=parse_instant(p["occurred_at"]),
            notified_at=parse_instant(p["notified_at"]),
            reporter=p.get("reporter", ""),
            fingerprint=p["fingerprint"],
            status=IncidentStatus.DUPLICATE,
            status_reason=p.get("reason", "重复通知"),
            duplicate_of=p["duplicate_of"],
        )
        self.incidents[inc.incident_id] = inc
        master = self.incidents.get(p["duplicate_of"])
        if master:
            master.corrections.append(
                {"kind": "duplicate_link", "duplicate_id": inc.incident_id,
                 "at": e.payload.get("at") or e.to_dict()["occurred_at"]}
            )

    def _on_scope_decided(self, e: Event) -> None:
        inc = self._incident(e)
        p = e.payload
        inc.in_scope = p["in_scope"]
        inc.scope_basis = p.get("basis", "")
        inc.within_notice = p.get("within_notice")
        if p.get("notice_deadline"):
            inc.notice_deadline = parse_instant(p["notice_deadline"])
        if p["in_scope"]:
            if p.get("within_notice") is False:
                inc.status = IncidentStatus.NOTICE_LATE
            else:
                inc.status = IncidentStatus.UNDER_REVIEW
        else:
            inc.status = IncidentStatus.OUT_OF_SCOPE
            inc.blocking_reasons.append(p.get("basis", "超出保单范围"))
        inc.status_reason = p.get("basis", "")

    def _on_material_received(self, e: Event) -> None:
        inc = self._incident(e)
        p = e.payload
        mat = Material(
            material_id=p["material_id"],
            kind=p["kind"],
            in_scope=p["in_scope"],
            reason=p.get("reason", ""),
            received_at=parse_instant(p["received_at"]),
            summary=p["summary"],
        )
        inc.materials.append(mat)
        if not p["in_scope"]:
            inc.blocking_reasons.append(f"材料超出范围: {p['material_id']} {p.get('reason','')}")

    def _on_material_corrected(self, e: Event) -> None:
        inc = self._incident(e)
        p = e.payload
        for mat in inc.materials:
            if mat.material_id == p["material_id"]:
                # 原始摘要保留，更正以追加形式挂在同一条材料下
                mat.corrections.append(
                    {"corrected_summary": p["corrected_summary"],
                     "reason": p.get("reason", ""),
                     "at": e.to_dict()["occurred_at"]}
                )
                break
        inc.corrections.append(
            {"kind": "material_correction", **{k: p[k] for k in
             ("material_id", "corrected_summary", "reason") if k in p}}
        )

    def _on_partial_loss_declared(self, e: Event) -> None:
        inc = self._incident(e)
        p = e.payload
        inc.claimed_amount = Decimal(str(p["claimed_amount"]))
        if inc.status != IncidentStatus.NOTICE_LATE:
            inc.status = IncidentStatus.PARTIAL_LOSS
        inc.status_reason = p.get("note", "部分损失申报")

    def _on_loss_adjusted(self, e: Event) -> None:
        inc = self._incident(e)
        p = e.payload
        inc.adjusted_amount = Decimal(str(p["adjusted_amount"]))
        inc.deductible_applied = Decimal(str(p.get("deductible_applied", "0")))
        inc.owner = p.get("adjuster", inc.owner)
        inc.status = IncidentStatus.ADJUSTED

    def _on_advance_authorized(self, e: Event) -> None:
        inc = self._incident(e)
        p = e.payload
        inc.advances.append(
            Advance(
                amount=Decimal(str(p["amount"])),
                cap=Decimal(str(p["cap"])),
                authorizer=p["authorizer"],
                authorized_at=parse_instant(p["authorized_at"]),
            )
        )
        inc.status = IncidentStatus.ADVANCE_AUTHORIZED

    def _on_payment_made(self, e: Event) -> None:
        inc = self._incident(e)
        p = e.payload
        inc.payments.append(
            {"payment_id": p["payment_id"], "amount": Decimal(str(p["amount"])),
             "kind": p.get("kind", "indemnity"), "at": parse_instant(p["paid_at"]),
             "reference": p.get("reference", "")}
        )
        inc.status = IncidentStatus.PAYMENT_PENDING if p.get("pending") else IncidentStatus.PAID

    def _on_acknowledgement_received(self, e: Event) -> None:
        inc = self._incident(e)
        p = e.payload
        inc.acknowledgements.append(
            {"from": p["from"], "kind": p.get("kind", "underwriter_receipt"),
             "at": parse_instant(p["received_at"]), "reference": p.get("reference", "")}
        )

    def _on_investigation_logged(self, e: Event) -> None:
        inc = self._incident(e)
        p = e.payload
        inc.investigation.append(
            {"node": p["node"], "at": parse_instant(p["at"]),
             "owner": p.get("owner", ""), "note": p.get("note", "")}
        )

    def _on_subrogation_opened(self, e: Event) -> None:
        inc = self._incident(e)
        p = e.payload
        inc.subrogation = {
            "responsible_party": p["responsible_party"],
            "target_amount": Decimal(str(p["target_amount"])),
            "opened_at": parse_instant(p["opened_at"]),
            "owner": p.get("owner"),
        }
        inc.owner = p.get("owner", inc.owner)
        inc.status = IncidentStatus.SUBROGATION

    def _on_recovery_received(self, e: Event) -> None:
        inc = self._incident(e)
        p = e.payload
        inc.recoveries.append(
            {"amount": Decimal(str(p["amount"])), "from": p["from"],
             "at": parse_instant(p["received_at"]),
             "late": p.get("late", False), "reference": p.get("reference", "")}
        )
        target = inc.subrogation["target_amount"] if inc.subrogation else None
        if target is not None and inc.recovered_total >= target:
            inc.status = IncidentStatus.RECOVERED
        inc.status_reason = "追偿回执迟到补录" if p.get("late") else inc.status_reason

    def _on_incident_closed(self, e: Event) -> None:
        inc = self._incident(e)
        inc.status = IncidentStatus.CLOSED
        inc.status_reason = e.payload.get("note", "")

    def _on_status_corrected(self, e: Event) -> None:
        inc = self._incident(e)
        p = e.payload
        inc.corrections.append(
            {"kind": "status", "from": p["from_status"], "to": p["to_status"],
             "reason": p.get("reason", ""), "at": e.to_dict()["occurred_at"]}
        )
        inc.status = IncidentStatus(p["to_status"])
        inc.status_reason = p.get("reason", inc.status_reason)
