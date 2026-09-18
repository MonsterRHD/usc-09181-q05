"""领域核心：海外保单风险处置台。

把保单范围、地区、免赔额、通知时限与事故证据统一到一条处置链上：
- 一切事实以追加事件写入，状态更正只追加新事件，原始文件摘要不可替换；
- 同一事故的重复报案并入主案，落在可解释状态 duplicate_notice；
- 跨时区报案按绝对时间归一化后再做排序与通知时限判定；
- 保单批改按生效时间决定是否适用于本案（生效晚于事故则不适用）；
- 超出保单范围的材料记录在案但不能推进赔付；
- 紧急预付必须记录授权人与上限，违规尝试留痕并拒绝；
- 赔付金额、责任人、下一步动作全部由处置链推导，可审计、可复盘。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .events import Event, EventStore, parse_ts, utcnow_iso


class DeskError(Exception):
    """业务规则拒绝。status 供 HTTP 层映射，detail 面向操作员。"""

    def __init__(self, code: str, detail: str, status: int = 409):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


# 案件主状态（全部由处置链推导，不直接落库）
STATUS_REPORTED = "reported"                # 已报案
STATUS_OUT_OF_SCOPE = "out_of_scope"        # 不在保单范围
STATUS_ACKNOWLEDGED = "acknowledged"        # 承保人已回执
STATUS_INVESTIGATING = "investigating"      # 调查中
STATUS_ASSESSED = "assessed"                # 损失已核定
STATUS_PREPAID = "prepaid"                  # 已紧急预付 / 部分赔付
STATUS_PAID = "paid"                        # 赔付完成

POLICY_STREAM_PREFIX = "policy:"

# 批改允许变更的保单条款
ENDORSEMENT_KEYS = {"regions", "coverages", "deductible", "limit", "notice_days"}

TERM_FIELDS = [
    "insured", "coverages", "regions", "deductible", "limit",
    "currency", "notice_days", "valid_from", "valid_to", "timezone",
]

# 材料更正允许补充的字段：原始文件摘要不可替换，只能追加更正说明
CORRECTION_KEYS = {"summary_note", "kind", "region", "peril", "amount", "currency"}


def _need(value, name: str):
    if value is None or value == "" or value == []:
        raise DeskError("missing_field", f"缺少必填字段: {name}", status=400)
    return value


def _parse(value: str, field: str):
    try:
        return parse_ts(value)
    except (TypeError, ValueError) as exc:
        raise DeskError("bad_timestamp", f"字段 {field} 不是带时区的 ISO 时间: {exc}", status=400)


def _check_amount(value, name: str, allow_zero: bool = False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeskError("bad_amount", f"{name} 必须为整数（最小货币单位）", status=400)
    if value < 0 or (value == 0 and not allow_zero):
        raise DeskError("bad_amount", f"{name} 必须{'非负' if allow_zero else '为正'}", status=400)
    return value


class Desk:
    """处置台应用服务：接收命令、追加事件、推导案件视图。"""

    def __init__(self, store: EventStore):
        self.store = store
        self._lock = threading.RLock()
        # 事故外部编号 -> 案件 id，用于重复报案归并；系统恢复后由事件重建
        self._case_by_ref: dict[str, str] = {}
        for e in self.store.events():
            if e.type == "incident_reported":
                self._case_by_ref[e.payload["incident_ref"]] = e.stream

    # ------------------------------------------------------------------ 保单

    def register_policy(self, *, actor: str, terms: dict, policy_id: str | None = None) -> dict:
        _need(actor, "actor")
        self._validate_terms(terms)
        policy_id = policy_id or uuid.uuid4().hex
        stream = POLICY_STREAM_PREFIX + policy_id
        with self._lock:
            if self.store.events(stream):
                raise DeskError("policy_exists", f"保单已存在: {policy_id}")
            self.store.append("policy_registered", stream, terms["valid_from"], actor,
                              {"policy_id": policy_id, "terms": dict(terms)})
        return self.policy_view(policy_id)

    def record_endorsement(self, policy_id: str, *, actor: str, changes: dict,
                           effective_at: str, note: str | None = None) -> dict:
        """登记保单批改。是否适用于某起事故，取决于生效时间与事故时间的先后。"""
        _need(actor, "actor")
        with self._lock:
            self._registered_terms(policy_id)
            unknown = set(changes or {}) - ENDORSEMENT_KEYS
            if unknown:
                raise DeskError("unknown_endorsement_field",
                                f"批改包含不支持的条款: {', '.join(sorted(unknown))}", status=400)
            _need(changes, "changes")
            _parse(effective_at, "effective_at")
            self._validate_endorsement_values(changes)
            self.store.append("endorsement_recorded", POLICY_STREAM_PREFIX + policy_id,
                              effective_at, actor,
                              {"policy_id": policy_id, "changes": dict(changes),
                               "effective_at": effective_at, "note": note})
        return self.policy_view(policy_id)

    def policy_view(self, policy_id: str) -> dict:
        events = self.store.chain(POLICY_STREAM_PREFIX + policy_id)
        reg = next((e for e in events if e.type == "policy_registered"), None)
        if reg is None:
            raise DeskError("policy_not_found", f"保单不存在: {policy_id}", status=404)
        endorsements = [
            {"effective_at": e.payload["effective_at"], "changes": e.payload["changes"],
             "note": e.payload.get("note"), "recorded_by": e.actor}
            for e in events if e.type == "endorsement_recorded"
        ]
        return {"policy_id": policy_id, "terms": reg.payload["terms"],
                "endorsements": endorsements}

    # ------------------------------------------------------------------ 报案

    def report_incident(self, *, actor: str, incident_ref: str, policy_id: str,
                        occurred_at: str, peril: str, region: str, handler: str,
                        reported_at: str | None = None, note: str | None = None,
                        portal: str | None = None) -> dict:
        """报案。同一事故（incident_ref）重复报案时并入主案，不重复立案。"""
        _need(actor, "actor")
        _need(incident_ref, "incident_ref")
        _need(peril, "peril")
        _need(region, "region")
        _need(handler, "handler")
        _parse(occurred_at, "occurred_at")
        reported_at = reported_at or utcnow_iso()
        _parse(reported_at, "reported_at")
        with self._lock:
            self._registered_terms(policy_id)  # 保单不存在则 404
            existing = self._case_by_ref.get(incident_ref)
            if existing:
                self.store.append("duplicate_notice", existing, reported_at, actor,
                                  {"incident_ref": incident_ref, "policy_id": policy_id,
                                   "occurred_at": occurred_at, "reported_at": reported_at,
                                   "handler": handler, "portal": portal, "note": note})
                view = self._fold(existing)
                view["deduplicated"] = True
                return view
            case_id = uuid.uuid4().hex
            self.store.append("incident_reported", case_id, occurred_at, actor,
                              {"incident_ref": incident_ref, "policy_id": policy_id,
                               "occurred_at": occurred_at, "reported_at": reported_at,
                               "peril": peril, "region": region,
                               "handler": handler, "note": note})
            self._case_by_ref[incident_ref] = case_id
            return self._fold(case_id)

    # ------------------------------------------------------------------ 处置链事件

    def record_receipt(self, case_id: str, *, actor: str, insurer: str,
                       ref: str | None = None, received_at: str | None = None) -> dict:
        """登记承保人回执。"""
        _need(actor, "actor")
        _need(insurer, "insurer")
        received_at = received_at or utcnow_iso()
        _parse(received_at, "received_at")
        with self._lock:
            self._report_or_raise(case_id)
            self.store.append("insurer_receipt", case_id, received_at, actor,
                              {"insurer": insurer, "ref": ref, "received_at": received_at})
            return self._fold(case_id)

    def record_milestone(self, case_id: str, *, actor: str, name: str,
                         note: str | None = None, reached_at: str | None = None) -> dict:
        """登记调查节点。"""
        _need(actor, "actor")
        _need(name, "name")
        reached_at = reached_at or utcnow_iso()
        _parse(reached_at, "reached_at")
        with self._lock:
            self._report_or_raise(case_id)
            self.store.append("investigation_milestone", case_id, reached_at, actor,
                              {"name": name, "note": note, "reached_at": reached_at})
            return self._fold(case_id)

    def submit_material(self, case_id: str, *, actor: str, kind: str, summary: str,
                        submitted_at: str | None = None, region: str | None = None,
                        peril: str | None = None, amount: int | None = None,
                        currency: str | None = None) -> dict:
        """登记客户材料。超出保单范围的材料留痕为 out_of_scope，不能推进赔付。"""
        _need(actor, "actor")
        _need(kind, "kind")
        _need(summary, "summary")
        submitted_at = submitted_at or utcnow_iso()
        _parse(submitted_at, "submitted_at")
        with self._lock:
            self._report_or_raise(case_id)
            evaluation = self._evaluate_submission(case_id, region, peril, currency)
            self.store.append("material_submitted", case_id, submitted_at, actor,
                              {"kind": kind, "summary": summary, "region": region,
                               "peril": peril, "amount": amount, "currency": currency,
                               "evaluation": evaluation})
            return self._fold(case_id)

    def correct_material(self, case_id: str, *, actor: str, material_event_id: str,
                         changes: dict, reason: str,
                         corrected_at: str | None = None) -> dict:
        """更正材料：以追加事件表示，原始文件摘要不可替换。"""
        _need(actor, "actor")
        _need(reason, "reason")
        _need(changes, "changes")
        if "summary" in changes:
            raise DeskError("summary_immutable",
                            "原始文件摘要不可替换，请以 summary_note 追加更正说明", status=400)
        unknown = set(changes) - CORRECTION_KEYS
        if unknown:
            raise DeskError("unknown_correction_field",
                            f"更正包含不支持的字段: {', '.join(sorted(unknown))}", status=400)
        corrected_at = corrected_at or utcnow_iso()
        _parse(corrected_at, "corrected_at")
        with self._lock:
            self._report_or_raise(case_id)
            target = next((e for e in self.store.events(case_id)
                           if e.event_id == material_event_id
                           and e.type == "material_submitted"), None)
            if target is None:
                raise DeskError("material_not_found",
                                f"材料不存在: {material_event_id}", status=404)
            self.store.append("material_corrected", case_id, corrected_at, actor,
                              {"material_event_id": material_event_id,
                               "changes": dict(changes), "reason": reason})
            return self._fold(case_id)

    def assess_loss(self, case_id: str, *, actor: str, amount: int, assessor: str,
                    partial: bool = False, currency: str | None = None,
                    assessed_at: str | None = None) -> dict:
        """损失核定。partial=True 表示部分损失，案件保持开放等待补充。"""
        _need(actor, "actor")
        _need(assessor, "assessor")
        _check_amount(amount, "核定金额", allow_zero=True)
        assessed_at = assessed_at or utcnow_iso()
        _parse(assessed_at, "assessed_at")
        with self._lock:
            self._report_or_raise(case_id)
            evaluation = self._evaluate_submission(case_id, currency=currency)
            self.store.append("loss_assessed", case_id, assessed_at, actor,
                              {"amount": amount, "assessor": assessor, "partial": bool(partial),
                               "currency": currency, "evaluation": evaluation})
            return self._fold(case_id)

    def authorize_prepayment(self, case_id: str, *, actor: str, amount: int,
                             authorizer: str | None = None, limit: int | None = None,
                             reason: str | None = None,
                             authorized_at: str | None = None) -> dict:
        """紧急预付：必须记录授权人与上限；违规尝试留痕为 prepayment_rejected。"""
        _need(actor, "actor")
        authorized_at = authorized_at or utcnow_iso()
        _parse(authorized_at, "authorized_at")
        with self._lock:
            self._report_or_raise(case_id)
            problem = None
            if not authorizer:
                problem = "紧急预付必须记录授权人"
            elif limit is None:
                problem = "紧急预付必须记录授权上限"
            else:
                try:
                    _check_amount(limit, "授权上限", allow_zero=True)
                    _check_amount(amount, "预付金额")
                except DeskError as exc:
                    problem = exc.detail
                else:
                    if amount > limit:
                        problem = f"预付金额 {amount} 超出授权上限 {limit}"
                    elif self._fold(case_id)["status"] == STATUS_OUT_OF_SCOPE:
                        problem = "案件不在保单范围内，不能预付"
            if problem:
                self.store.append("prepayment_rejected", case_id, authorized_at, actor,
                                  {"amount": amount, "authorizer": authorizer,
                                   "limit": limit, "reason": problem})
                raise DeskError("prepayment_rejected", problem)
            self.store.append("prepayment_authorized", case_id, authorized_at, actor,
                              {"amount": amount, "authorizer": authorizer,
                               "limit": limit, "reason": reason})
            return self._fold(case_id)

    def record_payout(self, case_id: str, *, actor: str, amount: int,
                      paid_at: str | None = None, ref: str | None = None) -> dict:
        """登记赔付。金额不得超过按处置链推导的应付余额。"""
        _need(actor, "actor")
        paid_at = paid_at or utcnow_iso()
        _parse(paid_at, "paid_at")
        with self._lock:
            self._report_or_raise(case_id)
            view = self._fold(case_id)
            outstanding = view["payout"]["outstanding"]
            problem = None
            if view["status"] == STATUS_OUT_OF_SCOPE:
                problem = "案件不在保单范围内，不能赔付"
            elif outstanding is None:
                problem = "损失尚未完成有效核定，不能赔付"
            else:
                try:
                    _check_amount(amount, "赔付金额")
                except DeskError as exc:
                    problem = exc.detail
                else:
                    if amount > outstanding:
                        problem = f"赔付金额 {amount} 超出应付余额 {outstanding}"
            if problem:
                self.store.append("payout_rejected", case_id, paid_at, actor,
                                  {"amount": amount, "reason": problem})
                raise DeskError("payout_rejected", problem)
            self.store.append("payout_made", case_id, paid_at, actor,
                              {"amount": amount, "ref": ref})
            return self._fold(case_id)

    def record_subrogation(self, case_id: str, *, actor: str, amount: int, payer: str,
                           ref: str | None = None,
                           received_at: str | None = None) -> dict:
        """登记追偿回执。赔付完成后迟到的回执落在可解释状态 late_subrogation_receipt。"""
        _need(actor, "actor")
        _need(payer, "payer")
        _check_amount(amount, "追偿金额")
        received_at = received_at or utcnow_iso()
        _parse(received_at, "received_at")
        with self._lock:
            self._report_or_raise(case_id)
            self.store.append("subrogation_receipt", case_id, received_at, actor,
                              {"amount": amount, "payer": payer, "ref": ref,
                               "received_at": received_at})
            return self._fold(case_id)

    # ------------------------------------------------------------------ 视图

    def case_view(self, case_id: str) -> dict:
        with self._lock:
            return self._fold(case_id)

    def case_chain(self, case_id: str) -> list[dict]:
        """处置链：按业务时间整理的全部事件，含被拒绝的尝试，供审计。"""
        with self._lock:
            self._report_or_raise(case_id)
            return [asdict(e) for e in self.store.chain(case_id)]

    def list_cases(self) -> list[dict]:
        """案件列表，按事故发生顺序整理。"""
        with self._lock:
            views = [self._fold(e.stream) for e in self.store.events()
                     if e.type == "incident_reported"]
            views.sort(key=lambda v: parse_ts(v["occurred_at"]).astimezone(timezone.utc))
            return [
                {"case_id": v["case_id"], "incident_ref": v["incident_ref"],
                 "policy_id": v["policy_id"], "status": v["status"],
                 "occurred_at": v["occurred_at"], "responsible": v["responsible"]}
                for v in views
            ]

    # ------------------------------------------------------------------ 内部：保单快照

    def _registered_terms(self, policy_id: str) -> dict:
        reg = next((e for e in self.store.events(POLICY_STREAM_PREFIX + policy_id)
                    if e.type == "policy_registered"), None)
        if reg is None:
            raise DeskError("policy_not_found", f"保单不存在: {policy_id}", status=404)
        return reg.payload["terms"]

    def _policy_snapshot(self, policy_id: str, at) -> tuple[dict, list[Event], list[Event]]:
        """事故时刻的保单条款快照：只应用生效时间不晚于事故时间的批改。"""
        terms = dict(self._registered_terms(policy_id))
        applied, pending = [], []
        for e in self.store.chain(POLICY_STREAM_PREFIX + policy_id):
            if e.type != "endorsement_recorded":
                continue
            if parse_ts(e.payload["effective_at"]) <= at:
                terms.update(e.payload["changes"])
                applied.append(e)
            else:
                pending.append(e)
        return terms, applied, pending

    def _incident_scope_reasons(self, report_payload: dict, terms: dict) -> list[str]:
        occurred = parse_ts(report_payload["occurred_at"])
        reasons = []
        if not (parse_ts(terms["valid_from"]) <= occurred <= parse_ts(terms["valid_to"])):
            reasons.append("事故时间不在保单有效期内")
        if report_payload["region"] not in terms["regions"]:
            reasons.append(f"地区 {report_payload['region']} 不在承保范围")
        if report_payload["peril"] not in terms["coverages"]:
            reasons.append(f"险种 {report_payload['peril']} 不在承保范围")
        return reasons

    def _evaluate_submission(self, case_id: str, region=None, peril=None,
                             currency=None) -> dict:
        """材料/核定的范围判定：超范围留痕，但不推进赔付。"""
        report = self._report_or_raise(case_id)
        terms, _, _ = self._policy_snapshot(
            report.payload["policy_id"], parse_ts(report.payload["occurred_at"]))
        reasons = list(self._incident_scope_reasons(report.payload, terms))
        if region is not None and region not in terms["regions"]:
            reasons.append(f"材料地区 {region} 超出保单范围")
        if peril is not None and peril not in terms["coverages"]:
            reasons.append(f"材料险种 {peril} 超出保单范围")
        if currency is not None and currency != terms["currency"]:
            reasons.append(f"币种 {currency} 与保单币种 {terms['currency']} 不符")
        return {"result": "out_of_scope" if reasons else "in_scope",
                "reason": "；".join(reasons)}

    def _report_or_raise(self, case_id: str) -> Event:
        report = next((e for e in self.store.events(case_id)
                       if e.type == "incident_reported"), None)
        if report is None:
            raise DeskError("case_not_found", f"案件不存在: {case_id}", status=404)
        return report

    # ------------------------------------------------------------------ 内部：状态推导

    def _fold(self, case_id: str) -> dict:
        chain = self.store.chain(case_id)
        report = next((e for e in chain if e.type == "incident_reported"), None)
        if report is None:
            raise DeskError("case_not_found", f"案件不存在: {case_id}", status=404)
        p = report.payload
        occurred_at = parse_ts(p["occurred_at"])
        reported_at = parse_ts(p["reported_at"])
        terms, applied_endorsements, pending_endorsements = self._policy_snapshot(
            p["policy_id"], occurred_at)

        notices: dict[str, dict] = {}

        def notice(code: str, detail: str, at: str):
            existing = notices.get(code)
            if existing:
                existing["count"] += 1
            else:
                notices[code] = {"code": code, "detail": detail, "at": at, "count": 1}

        # —— 可解释状态：范围 / 跨时区 / 通知时限 / 批改适用性 ——
        scope_reasons = self._incident_scope_reasons(p, terms)
        in_scope = not scope_reasons
        for reason in scope_reasons:
            notice("incident_out_of_scope", reason, p["occurred_at"])

        policy_tz = ZoneInfo(terms["timezone"])
        if occurred_at.utcoffset() != occurred_at.astimezone(policy_tz).utcoffset():
            notice("cross_timezone_normalized",
                   f"事故时间 {p['occurred_at']} 与保单时区 {terms['timezone']} 不一致，"
                   f"已按绝对时间归一化（{occurred_at.astimezone(policy_tz).isoformat()}）"
                   f"后再排序与计算时限",
                   p["occurred_at"])
        if reported_at - occurred_at > timedelta(days=terms["notice_days"]):
            notice("late_notice",
                   f"报案时间超出保单约定的 {terms['notice_days']} 天通知时限",
                   p["reported_at"])
        for e in applied_endorsements:
            notice("endorsement_applied",
                   f"批改（{e.payload.get('note') or e.event_id}）生效于 "
                   f"{e.payload['effective_at']}，早于事故时间，已适用于本案",
                   e.payload["effective_at"])
        for e in pending_endorsements:
            notice("endorsement_not_applicable",
                   f"批改（{e.payload.get('note') or e.event_id}）生效于 "
                   f"{e.payload['effective_at']}，晚于事故时间，不适用于本案",
                   e.payload["effective_at"])

        # —— 沿处置链推导 ——
        base_status = STATUS_REPORTED
        materials: list[dict] = []
        corrections: dict[str, list[dict]] = {}
        receipts, milestones, duplicates = [], [], []
        prepayments, payouts, recoveries, rejected = [], [], [], []
        assessed_loss = None
        assessment_partial = False
        assessor = None
        assessed_at = None

        for e in chain:
            pl = e.payload
            if e.type == "duplicate_notice":
                duplicates.append(e)
                where = pl.get("portal") or "另一承保人门户"
                notice("duplicate_notice",
                       f"同一事故经{where}重复报案，已并入本案，不重复立案",
                       pl.get("reported_at", e.business_time))
            elif e.type == "insurer_receipt":
                receipts.append(e)
                if in_scope and base_status == STATUS_REPORTED:
                    base_status = STATUS_ACKNOWLEDGED
            elif e.type == "investigation_milestone":
                milestones.append(e)
                if in_scope and base_status in (STATUS_REPORTED, STATUS_ACKNOWLEDGED):
                    base_status = STATUS_INVESTIGATING
            elif e.type == "material_submitted":
                materials.append(e)
                if pl["evaluation"]["result"] == "out_of_scope":
                    notice("material_out_of_scope",
                           f"材料《{pl['summary']}》未纳入赔付依据："
                           f"{pl['evaluation']['reason']}",
                           e.business_time)
            elif e.type == "material_corrected":
                corrections.setdefault(pl["material_event_id"], []).append(pl)
            elif e.type == "loss_assessed":
                if pl["evaluation"]["result"] == "in_scope":
                    assessed_loss = pl["amount"]
                    assessment_partial = bool(pl.get("partial"))
                    assessor = pl["assessor"]
                    assessed_at = e.business_time
                    if in_scope:
                        base_status = STATUS_ASSESSED
                    if pl.get("partial"):
                        notice("partial_loss",
                               "本次核定为部分损失，案件保持开放，可继续补充材料与核定",
                               e.business_time)
                else:
                    notice("assessment_out_of_scope",
                           f"损失核定未纳入赔付依据：{pl['evaluation']['reason']}",
                           e.business_time)
            elif e.type == "prepayment_authorized":
                prepayments.append(e)
            elif e.type == "payout_made":
                payouts.append(e)
            elif e.type == "subrogation_receipt":
                recoveries.append(e)
            elif e.type in ("prepayment_rejected", "payout_rejected"):
                rejected.append(e)

        # —— 赔付推导：min(核定损失, 保额) - 免赔额，再扣已预付/已赔付 ——
        prepaid_total = sum(e.payload["amount"] for e in prepayments)
        paid_out_total = sum(e.payload["amount"] for e in payouts)
        paid_total = prepaid_total + paid_out_total
        recovered_total = sum(e.payload["amount"] for e in recoveries)
        if in_scope and assessed_loss is not None:
            covered_loss = min(assessed_loss, terms["limit"])
            payable = max(0, covered_loss - terms["deductible"])
            outstanding = max(0, payable - paid_total)
        else:
            covered_loss = payable = outstanding = None

        if not in_scope:
            status = STATUS_OUT_OF_SCOPE
        elif payable is not None and not assessment_partial and paid_total >= payable:
            status = STATUS_PAID
        elif paid_total > 0:
            status = STATUS_PREPAID
        else:
            status = base_status

        # —— 追偿回执迟到：赔付结清之后才到账 ——
        late_recovery = False
        if recoveries and status == STATUS_PAID:
            paid_moment = None
            cumulative = 0
            for e in chain:
                if e.type in ("prepayment_authorized", "payout_made"):
                    cumulative += e.payload["amount"]
                    if cumulative >= payable:
                        paid_moment = parse_ts(e.business_time)
                        break
            if paid_moment is None:  # 应付为 0，核定即结清
                paid_moment = parse_ts(assessed_at)
            late_recovery = any(parse_ts(e.business_time) > paid_moment for e in recoveries)
            if late_recovery:
                notice("late_subrogation_receipt",
                       "追偿回执在赔付结清后迟到，已补登追偿台账，赔付结果不变",
                       recoveries[-1].business_time)

        # —— 下一步动作 ——
        if status == STATUS_OUT_OF_SCOPE:
            next_actions = ["核对保单范围与批改", "必要时按正确保单重新报案"]
        elif status == STATUS_REPORTED:
            next_actions = ["等待承保人回执"]
        elif status == STATUS_ACKNOWLEDGED:
            next_actions = ["提交事故证据材料", "记录调查节点"]
        elif status == STATUS_INVESTIGATING:
            next_actions = ["完成损失核定"]
        elif status == STATUS_ASSESSED:
            next_actions = (["补充最终损失核定"] if assessment_partial
                            else ["安排赔付", "必要时登记紧急预付"])
        elif status == STATUS_PREPAID:
            next_actions = ["结算剩余赔付"]
        else:
            next_actions = (["核对迟到的追偿回执并更新追偿台账", "结案归档"]
                            if late_recovery else ["跟踪追偿", "结案归档"])
        if status not in (STATUS_PAID, STATUS_OUT_OF_SCOPE):
            if "material_out_of_scope" in notices:
                next_actions = next_actions + ["补充保单范围内的有效材料"]
            if "late_notice" in notices:
                next_actions = next_actions + ["向承保人说明迟报原因"]

        material_views = []
        for e in materials:
            entry = {"event_id": e.event_id, "kind": e.payload["kind"],
                     "summary": e.payload["summary"],
                     "evaluation": e.payload["evaluation"],
                     "submitted_at": e.business_time, "actor": e.actor}
            if e.event_id in corrections:
                entry["corrections"] = corrections[e.event_id]
            material_views.append(entry)

        return {
            "case_id": case_id,
            "incident_ref": p["incident_ref"],
            "policy_id": p["policy_id"],
            "status": status,
            "responsible": p["handler"],
            "occurred_at": p["occurred_at"],
            "reported_at": p["reported_at"],
            "region": p["region"],
            "peril": p["peril"],
            "policy_snapshot": {
                "coverages": terms["coverages"], "regions": terms["regions"],
                "deductible": terms["deductible"], "limit": terms["limit"],
                "currency": terms["currency"], "notice_days": terms["notice_days"],
                "timezone": terms["timezone"],
            },
            "notices": list(notices.values()),
            "payout": {
                "currency": terms["currency"],
                "assessed_loss": assessed_loss,
                "assessment_partial": assessment_partial,
                "assessor": assessor,
                "deductible": terms["deductible"],
                "policy_limit": terms["limit"],
                "covered_loss": covered_loss,
                "payable": payable,
                "prepaid_total": prepaid_total,
                "paid_out_total": paid_out_total,
                "outstanding": outstanding,
                "recovered_total": recovered_total,
                "authorizations": [
                    {"amount": e.payload["amount"], "authorizer": e.payload["authorizer"],
                     "limit": e.payload["limit"], "reason": e.payload.get("reason"),
                     "at": e.business_time}
                    for e in prepayments
                ],
            },
            "materials": material_views,
            "receipts": [{"insurer": e.payload["insurer"], "ref": e.payload.get("ref"),
                          "received_at": e.business_time} for e in receipts],
            "milestones": [{"name": e.payload["name"], "note": e.payload.get("note"),
                            "reached_at": e.business_time} for e in milestones],
            "duplicate_reports": len(duplicates),
            "rejected_attempts": len(rejected),
            "next_actions": next_actions,
            "chain_length": len(chain),
        }

    # ------------------------------------------------------------------ 内部：校验

    def _validate_terms(self, terms: dict):
        for field in TERM_FIELDS:
            _need(terms.get(field), field)
        if not isinstance(terms["coverages"], list) or not all(terms["coverages"]):
            raise DeskError("bad_terms", "coverages 必须为非空数组", status=400)
        if not isinstance(terms["regions"], list) or not all(terms["regions"]):
            raise DeskError("bad_terms", "regions 必须为非空数组", status=400)
        _check_amount(terms["deductible"], "免赔额", allow_zero=True)
        _check_amount(terms["limit"], "保额", allow_zero=True)
        _check_amount(terms["notice_days"], "通知时限天数", allow_zero=True)
        _parse(terms["valid_from"], "valid_from")
        _parse(terms["valid_to"], "valid_to")
        if parse_ts(terms["valid_from"]) >= parse_ts(terms["valid_to"]):
            raise DeskError("bad_terms", "valid_from 必须早于 valid_to", status=400)
        try:
            ZoneInfo(terms["timezone"])
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            raise DeskError("bad_terms", f"未知时区: {terms['timezone']}", status=400)

    def _validate_endorsement_values(self, changes: dict):
        if "deductible" in changes:
            _check_amount(changes["deductible"], "免赔额", allow_zero=True)
        if "limit" in changes:
            _check_amount(changes["limit"], "保额", allow_zero=True)
        if "notice_days" in changes:
            _check_amount(changes["notice_days"], "通知时限天数", allow_zero=True)
        for key in ("regions", "coverages"):
            if key in changes and (not isinstance(changes[key], list)
                                   or not all(changes[key])):
                raise DeskError("bad_endorsement", f"{key} 必须为非空数组", status=400)
