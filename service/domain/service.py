"""海外保单风险处置台：应用服务。

所有命令先做规则校验、再追加事件；拒绝同样落审计事件（rejected），保证
“谁在何时因为什么不能推进”可追溯。读模型每次从事件流重放，天然支持复盘。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from datetime import timedelta

from .errors import DomainError
from .events import parse_instant, instant_str
from .models import IncidentStatus
from .projection import Projection
from .store import EventStore

# 各险种认可的事故证据/材料类型；不在表内即“超出范围的材料”
EVIDENCE_KINDS: dict[str, set[str]] = {
    "cargo": {"cargo_survey", "bill_of_lading", "commercial_invoice", "damage_photo"},
    "political_risk": {"government_decree", "port_authority_notice", "customs_detention", "force_majeure_cert"},
}

TERMINAL = {IncidentStatus.PAID, IncidentStatus.RECOVERED, IncidentStatus.CLOSED}


@dataclass
class AuditView:
    """复盘视图：处置链 + 按事故发生序排列 + 审计结论。"""
    chain: list[dict]
    incidents: list[dict]

    def as_dict(self) -> dict:
        return {"chain": self.chain, "incidents": self.incidents}


class DeskService:
    def __init__(self, store: EventStore):
        self.store = store

    # ---- 内部工具 ----------------------------------------------------------
    def _project(self) -> Projection:
        proj = Projection()
        proj.rebuild(self.store.all_events())
        return proj

    def _incident(self, proj: Projection, incident_id: str):
        inc = proj.incidents.get(incident_id)
        if not inc:
            raise DomainError("incident_not_found", f"事故不存在: {incident_id}")
        return inc

    def _policy(self, proj: Projection, policy_id: str):
        pol = proj.policies.get(policy_id)
        if not pol:
            raise DomainError("policy_not_found", f"保单不存在: {policy_id}")
        return pol

    def _reject(self, stream_id: str, code: str, message: str, ctx: dict | None = None):
        self.store.append(
            stream_id, "rejected",
            {"code": code, "message": message, **(ctx or {})},
        )
        raise DomainError(code, message)

    # ---- 保单与批改 --------------------------------------------------------
    def register_policy(self, policy_id: str, *, coverages: list[str], regions: list[str],
                        deductibles: dict[str, str] | None = None,
                        notice_limits_hours: dict[str, int] | None = None,
                        currency: str = "USD") -> dict:
        proj = self._project()
        if policy_id in proj.policies:
            raise DomainError("policy_exists", f"保单已登记: {policy_id}")
        evt = self.store.append(policy_id, "policy_registered", {
            "coverages": sorted(coverages), "regions": sorted(regions),
            "deductibles": deductibles or {},
            "notice_limits_hours": notice_limits_hours or {},
            "currency": currency,
        })
        return evt.to_dict()

    def record_endorsement(self, policy_id: str, endorsement_id: str, *,
                           effective_from: str, coverages_added: list[str] | None = None,
                           regions_added: list[str] | None = None, note: str = "") -> dict:
        proj = self._project()
        self._policy(proj, policy_id)
        evt = self.store.append(policy_id, "endorsement_recorded", {
            "endorsement_id": endorsement_id,
            "effective_from": instant_str(parse_instant(effective_from)),
            "coverages_added": coverages_added or [],
            "regions_added": regions_added or [],
            "note": note,
        })
        return evt.to_dict()

    # ---- 报案 --------------------------------------------------------------
    @staticmethod
    def fingerprint(*, policy_id: str, loss_type: str, occurred_at: str,
                    vessel_or_ref: str) -> str:
        # 同一保单 + 险种 + 事故时间 + 航次/标的 的通知视为同一事故
        return f"{policy_id}|{loss_type}|{instant_str(parse_instant(occurred_at))}|{vessel_or_ref}"

    def report_incident(self, incident_id: str, *, policy_id: str, loss_type: str,
                        occurred_at: str, notified_at: str, reporter: str = "",
                        region: str = "", vessel_or_ref: str = "",
                        client_event_id: str | None = None) -> dict:
        """登记报案。重复指纹 -> DUPLICATE 并并入主事故，不产生第二条赔付线。"""
        # 断网补传：同一客户端事件重放返回首次结果，不重复登记
        if client_event_id:
            prior = self.store.lookup_client_event_id(client_event_id)
            if prior:
                data = prior.to_dict()
                if prior.type == "duplicate_reported":
                    data["duplicate_of"] = prior.payload["duplicate_of"]
                return data
        occ = parse_instant(occurred_at)
        notif = parse_instant(notified_at)
        if notif < occ:
            raise DomainError("bad_time", "通知时间早于事故发生时间")
        fp = self.fingerprint(
            policy_id=policy_id, loss_type=loss_type,
            occurred_at=occurred_at, vessel_or_ref=vessel_or_ref,
        )
        with self.store.transaction():
            proj = self._project()
            self._policy(proj, policy_id)
            if incident_id in proj.incidents:
                raise DomainError("incident_exists", f"事故已登记: {incident_id}")

            master_id = proj.fingerprints.get(fp)
            if master_id:
                evt = self.store.append(
                    incident_id, "duplicate_reported",
                    {"policy_id": policy_id, "loss_type": loss_type, "region": region,
                     "occurred_at": instant_str(occ), "notified_at": instant_str(notif),
                     "reporter": reporter, "fingerprint": fp,
                     "duplicate_of": master_id,
                     "reason": "同一事故的重复通知（跨承保人门户/跨时区重复报案）"},
                    client_event_id=client_event_id,
                )
                return {**evt.to_dict(), "duplicate_of": master_id}

            evt = self.store.append(
                incident_id, "incident_reported",
                {"policy_id": policy_id, "loss_type": loss_type, "region": region,
                 "occurred_at": instant_str(occ), "notified_at": instant_str(notif),
                 "reporter": reporter, "fingerprint": fp, "vessel_or_ref": vessel_or_ref,
                 "status": IncidentStatus.PENDING_SCOPE.value,
                 "status_reason": "已登记，等待保单范围与通知时限判定"},
                client_event_id=client_event_id,
            )
            return evt.to_dict()

    # ---- 范围研判 ----------------------------------------------------------
    def decide_scope(self, incident_id: str) -> dict:
        proj = self._project()
        inc = self._incident(proj, incident_id)
        if inc.status == IncidentStatus.DUPLICATE:
            raise DomainError("duplicate_incident",
                             f"重复事故不单独研判，请跟随主事故 {inc.duplicate_of}")
        policy = self._policy(proj, inc.policy_id)

        covered = inc.loss_type in policy.coverages_at(inc.occurred_at)
        region_ok = not policy.regions_at(inc.occurred_at) or inc.region in policy.regions_at(inc.occurred_at)
        in_scope = covered and region_ok

        limit_h = policy.notice_limit_hours(inc.loss_type)
        within_notice, deadline = None, None
        if limit_h is not None:
            deadline = inc.occurred_at + timedelta(hours=limit_h)
            within_notice = inc.notified_at <= deadline

        basis_parts = []
        if not covered:
            basis_parts.append(f"事故发生时险种 {inc.loss_type} 不在保单/已生效批改范围内")
        if not region_ok:
            basis_parts.append(f"地区 {inc.region or '未知'} 不在承保地区")
        if within_notice is False:
            basis_parts.append(
                f"超出 {limit_h} 小时通知时限（截止 {instant_str(deadline)}，实际报案 {instant_str(inc.notified_at)}）"
            )
        basis = "；".join(basis_parts) or (
            "范围成立，材料审单据中" if within_notice is not False
            else "范围成立"
        )

        evt = self.store.append(incident_id, "scope_decided", {
            "in_scope": in_scope, "within_notice": within_notice,
            "notice_deadline": instant_str(deadline) if deadline else None,
            "basis": basis,
        })
        return evt.to_dict()

    # ---- 材料与更正 --------------------------------------------------------
    def receive_material(self, incident_id: str, material_id: str, *, kind: str,
                         summary: str, received_at: str) -> dict:
        proj = self._project()
        inc = self._incident(proj, incident_id)
        if inc.status == IncidentStatus.DUPLICATE:
            raise DomainError("duplicate_incident", "材料请提交到主事故")
        allowed = EVIDENCE_KINDS.get(inc.loss_type, set())
        in_scope = kind in allowed
        reason = "" if in_scope else f"{kind} 不属于险种 {inc.loss_type} 认可的证据类型，不能推进赔付"
        evt = self.store.append(incident_id, "material_received", {
            "material_id": material_id, "kind": kind, "in_scope": in_scope,
            "reason": reason, "summary": summary,
            "received_at": instant_str(parse_instant(received_at)),
        })
        if not in_scope:
            # 记录但拒绝推进：赔付前校验会看到这条阻断原因
            self.store.append(incident_id, "rejected", {
                "code": "material_out_of_scope",
                "message": reason, "material_id": material_id,
            })
        return evt.to_dict()

    def correct_material(self, incident_id: str, material_id: str, *,
                         corrected_summary: str, reason: str) -> dict:
        """状态/摘要更正：只追加，不替换原始摘要。"""
        proj = self._project()
        inc = self._incident(proj, incident_id)
        if not any(m.material_id == material_id for m in inc.materials):
            raise DomainError("material_not_found", f"材料不存在: {material_id}")
        evt = self.store.append(incident_id, "material_corrected", {
            "material_id": material_id,
            "corrected_summary": corrected_summary, "reason": reason,
        })
        return evt.to_dict()

    # ---- 定损 / 部分损失 ---------------------------------------------------
    def declare_partial_loss(self, incident_id: str, *, claimed_amount: str,
                             note: str = "") -> dict:
        proj = self._project()
        inc = self._incident(proj, incident_id)
        self._require_open_claim(inc)
        if inc.in_scope is False:
            self._reject(incident_id, "out_of_scope", "超出范围的事故不能申报损失")
        evt = self.store.append(incident_id, "partial_loss_declared", {
            "claimed_amount": str(Decimal(claimed_amount)), "note": note,
        })
        return evt.to_dict()

    def adjust_loss(self, incident_id: str, *, adjusted_amount: str, adjuster: str) -> dict:
        with self.store.transaction():
            proj = self._project()
            inc = self._incident(proj, incident_id)
            self._require_open_claim(inc)
            if inc.in_scope is not True:
                self._reject(incident_id, "not_in_scope", "范围未确认成立，不能定损")
            if inc.has_blocking_material:
                self._reject(incident_id, "blocking_material",
                             "存在超出范围的材料，定损冻结",
                             {"blocking_reasons": inc.blocking_reasons})
            policy = self._policy(proj, inc.policy_id)
            adjusted = Decimal(adjusted_amount)
            deductible = policy.deductible_for(inc.loss_type)
            if inc.claimed_amount is not None and adjusted > inc.claimed_amount:
                self._reject(incident_id, "adjusted_exceeds_claim",
                             "定损金额不得超过申报金额")
            evt = self.store.append(incident_id, "loss_adjusted", {
                "adjusted_amount": str(adjusted),
                "deductible_applied": str(deductible),
                "adjuster": adjuster,
            })
            return evt.to_dict()

    # ---- 紧急预付 ----------------------------------------------------------
    def authorize_advance(self, incident_id: str, *, amount: str, cap: str,
                          authorizer: str, authorized_at: str) -> dict:
        """紧急预付必须记录授权人、授权上限；累计预付不得超过授权上限与可赔余额。"""
        if not authorizer:
            raise DomainError("authorizer_required", "紧急预付必须记录授权人")
        amount_d, cap_d = Decimal(amount), Decimal(cap)
        if amount_d <= 0 or cap_d <= 0:
            raise DomainError("bad_amount", "金额必须为正")
        with self.store.transaction():
            proj = self._project()
            inc = self._incident(proj, incident_id)
            self._require_open_claim(inc)
            if inc.in_scope is not True:
                self._reject(incident_id, "not_in_scope", "范围未成立，不能授权预付")
            prior = sum((a.amount for a in inc.advances), Decimal("0"))
            if prior + amount_d > cap_d:
                self._reject(incident_id, "advance_cap_exceeded",
                             f"累计预付 {prior + amount_d} 超过授权上限 {cap_d}",
                             {"prior": str(prior), "cap": str(cap_d)})
            if inc.adjusted_amount is not None:
                policy = self._policy(proj, inc.policy_id)
                payable = inc.adjusted_amount - policy.deductible_for(inc.loss_type)
                if prior + amount_d > payable:
                    self._reject(incident_id, "advance_exceeds_payable",
                                 f"累计预付超过可赔余额 {payable}")
            evt = self.store.append(incident_id, "advance_authorized", {
                "amount": str(amount_d), "cap": str(cap_d),
                "authorizer": authorizer,
                "authorized_at": instant_str(parse_instant(authorized_at)),
            })
            return evt.to_dict()

    # ---- 赔付 --------------------------------------------------------------
    def pay(self, incident_id: str, payment_id: str, *, amount: str, paid_at: str,
            kind: str = "indemnity", reference: str = "", pending: bool = False) -> dict:
        with self.store.transaction():
            proj = self._project()
            inc = self._incident(proj, incident_id)
            if inc.status == IncidentStatus.DUPLICATE:
                self._reject(incident_id, "duplicate_incident",
                             "重复事故不得单独赔付", {"follow": inc.duplicate_of})
            if inc.in_scope is False:
                self._reject(incident_id, "out_of_scope", "超出保单范围，不能赔付")
            if inc.has_blocking_material:
                self._reject(incident_id, "blocking_material",
                             "存在超出范围的材料，赔付冻结",
                             {"blocking_reasons": inc.blocking_reasons})
            if inc.adjusted_amount is None:
                self._reject(incident_id, "not_adjusted", "尚未定损，不能赔付")
            policy = self._policy(proj, inc.policy_id)
            deductible = policy.deductible_for(inc.loss_type)
            advances = sum((a.amount for a in inc.advances), Decimal("0"))
            payable = inc.adjusted_amount - deductible - advances
            amount_d = Decimal(amount)
            already = sum((Decimal(p["amount"]) for p in inc.payments), Decimal("0"))
            if already + amount_d > payable:
                self._reject(incident_id, "payment_exceeds_payable",
                             f"本次/累计赔付超过可赔余额 {payable}（已付 {already}，预付 {advances}，免赔额 {deductible}）",
                             {"payable": str(payable), "already_paid": str(already)})
            evt = self.store.append(incident_id, "payment_made", {
                "payment_id": payment_id, "amount": str(amount_d), "kind": kind,
                "paid_at": instant_str(parse_instant(paid_at)),
                "reference": reference, "pending": pending,
            })
            return evt.to_dict()

    # ---- 回执 / 调查 / 追偿 ------------------------------------------------
    def receive_acknowledgement(self, incident_id: str, *, from_: str,
                                received_at: str, kind: str = "underwriter_receipt",
                                reference: str = "") -> dict:
        self._incident(self._project(), incident_id)
        evt = self.store.append(incident_id, "acknowledgement_received", {
            "from": from_, "kind": kind,
            "received_at": instant_str(parse_instant(received_at)),
            "reference": reference,
        })
        return evt.to_dict()

    def log_investigation(self, incident_id: str, *, node: str, at: str,
                          owner: str = "", note: str = "") -> dict:
        self._incident(self._project(), incident_id)
        evt = self.store.append(incident_id, "investigation_logged", {
            "node": node, "at": instant_str(parse_instant(at)),
            "owner": owner, "note": note,
        })
        return evt.to_dict()

    def open_subrogation(self, incident_id: str, *, responsible_party: str,
                         target_amount: str, opened_at: str, owner: str = "") -> dict:
        proj = self._project()
        inc = self._incident(proj, incident_id)
        evt = self.store.append(incident_id, "subrogation_opened", {
            "responsible_party": responsible_party,
            "target_amount": str(Decimal(target_amount)),
            "opened_at": instant_str(parse_instant(opened_at)),
            "owner": owner,
        })
        return evt.to_dict()

    def receive_recovery(self, incident_id: str, *, amount: str, from_: str,
                         received_at: str, late: bool = False, reference: str = "") -> dict:
        """追偿回执，迟到回执以 late=True 补录，追加在处置链末端但归属事故不变。"""
        proj = self._project()
        inc = self._incident(proj, incident_id)
        if not inc.subrogation:
            self._reject(incident_id, "subrogation_not_open", "未开立追偿，不能登记回执")
        evt = self.store.append(incident_id, "recovery_received", {
            "amount": str(Decimal(amount)), "from": from_,
            "received_at": instant_str(parse_instant(received_at)),
            "late": late, "reference": reference,
        })
        return evt.to_dict()

    def close_incident(self, incident_id: str, *, note: str = "") -> dict:
        inc = self._incident(self._project(), incident_id)
        evt = self.store.append(incident_id, "incident_closed", {"note": note})
        return evt.to_dict()

    def correct_status(self, incident_id: str, *, to_status: str, reason: str) -> dict:
        proj = self._project()
        inc = self._incident(proj, incident_id)
        if not any(s.value == to_status for s in IncidentStatus):
            raise DomainError("bad_status", f"未知状态: {to_status}")
        evt = self.store.append(incident_id, "status_corrected", {
            "from_status": inc.status.value, "to_status": to_status, "reason": reason,
        })
        return evt.to_dict()

    # ---- 查询 / 复盘 -------------------------------------------------------
    def get_incident(self, incident_id: str) -> dict:
        inc = self._incident(self._project(), incident_id)
        return self._incident_dto(inc)

    def list_incidents(self, order: str = "occurrence") -> list[dict]:
        proj = self._project()
        incs = proj.incidents_by_occurrence() if order == "occurrence" \
            else sorted(proj.incidents.values(), key=lambda i: i.incident_id)
        return [self._incident_dto(i) for i in incs]

    def audit_trail(self) -> AuditView:
        """业务复盘：完整处置链 + 每起事故的金额、责任人、下一步动作。"""
        proj = self._project()
        chain = [e.to_dict() for e in self.store.all_events()]
        incidents = [self._incident_dto(i) for i in proj.incidents_by_occurrence()]
        return AuditView(chain=chain, incidents=incidents)

    # ---- DTO & 规则片段 ----------------------------------------------------
    @staticmethod
    def _require_open_claim(inc) -> None:
        if inc.status == IncidentStatus.DUPLICATE:
            raise DomainError("duplicate_incident",
                             f"重复事故，请在主事故 {inc.duplicate_of} 上操作")
        if inc.status in TERMINAL:
            raise DomainError("incident_closed", "事故已终结，不能变更；如需更正请追加更正事件")

    @staticmethod
    def _incident_dto(inc) -> dict:
        payable = None
        if inc.adjusted_amount is not None:
            # 免赔额在定损事件中已快照
            payable = inc.adjusted_amount - (inc.deductible_applied or Decimal("0"))
        advances = sum((a.amount for a in inc.advances), Decimal("0"))
        outstanding = None
        if payable is not None:
            outstanding = payable - inc.paid_total - advances
        return {
            "incident_id": inc.incident_id,
            "policy_id": inc.policy_id,
            "loss_type": inc.loss_type,
            "region": inc.region,
            "occurred_at": instant_str(inc.occurred_at) if inc.occurred_at else None,
            "notified_at": instant_str(inc.notified_at) if inc.notified_at else None,
            "reporter": inc.reporter,
            "status": inc.status.value,
            "status_reason": inc.status_reason,
            "duplicate_of": inc.duplicate_of,
            "owner": inc.owner,
            "in_scope": inc.in_scope,
            "within_notice": inc.within_notice,
            "claimed_amount": str(inc.claimed_amount) if inc.claimed_amount is not None else None,
            "adjusted_amount": str(inc.adjusted_amount) if inc.adjusted_amount is not None else None,
            "deductible_applied": str(inc.deductible_applied) if inc.deductible_applied is not None else None,
            "advances_total": str(advances),
            "paid_total": str(inc.paid_total),
            "recovered_total": str(inc.recovered_total),
            "outstanding": str(outstanding) if outstanding is not None else None,
            "blocking_reasons": list(inc.blocking_reasons),
            "next_action": DeskService._next_action(inc),
            "materials": [
                {"material_id": m.material_id, "kind": m.kind, "in_scope": m.in_scope,
                 "reason": m.reason, "summary": m.summary,
                 "corrections": m.corrections}
                for m in inc.materials
            ],
            "advances": [
                {"amount": str(a.amount), "cap": str(a.cap), "authorizer": a.authorizer,
                 "authorized_at": instant_str(a.authorized_at)}
                for a in inc.advances
            ],
            "acknowledgements": inc.acknowledgements,
            "investigation": inc.investigation,
            "subrogation": (
                {"responsible_party": inc.subrogation["responsible_party"],
                 "target_amount": str(inc.subrogation["target_amount"]),
                 "owner": inc.subrogation.get("owner")}
                if inc.subrogation else None
            ),
            "recoveries": [
                {"amount": str(r["amount"]), "from": r["from"],
                 "received_at": instant_str(r["at"]), "late": r["late"]}
                for r in inc.recoveries
            ],
            "corrections": inc.corrections,
        }

    @staticmethod
    def _next_action(inc) -> str:
        st = inc.status
        if st == IncidentStatus.DUPLICATE:
            return f"跟随主事故 {inc.duplicate_of}，不在本副本上操作"
        if st == IncidentStatus.PENDING_SCOPE or st == IncidentStatus.REGISTERED:
            return "执行保单范围与通知时限研判 (decide_scope)"
        if st == IncidentStatus.OUT_OF_SCOPE:
            return "赔付冻结；如客户有批改/新证据，追加材料后由主管发起状态更正"
        if st == IncidentStatus.NOTICE_LATE:
            return "提交核保人对迟延通知的裁量，再决定是否继续定损"
        if st == IncidentStatus.UNDER_REVIEW:
            return "收集认可证据并定损；超出范围材料需撤回"
        if st == IncidentStatus.PARTIAL_LOSS:
            return "理赔员完成定损 (adjust_loss)"
        if st == IncidentStatus.ADJUSTED:
            return "安排赔付；如需紧急预付先登记授权人与上限"
        if st == IncidentStatus.ADVANCE_AUTHORIZED:
            return "凭授权预付，随后完成定损/尾款赔付"
        if st == IncidentStatus.PAYMENT_PENDING:
            return "跟进付款通道确认到账"
        if st == IncidentStatus.PAID:
            return "向责任方开立追偿 (open_subrogation)"
        if st == IncidentStatus.SUBROGATION:
            due = inc.subrogation["target_amount"] - inc.recovered_total
            return f"催收追偿款，待收 {due}"
        if st == IncidentStatus.RECOVERED:
            return "追偿款已达目标金额，可结案 (close)"
        if st == IncidentStatus.CLOSED:
            return "已结案；任何更正只能追加更正事件"
        return "人工研判"
