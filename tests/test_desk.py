"""领域核心测试：覆盖业务复盘要求的场景。

- 并发提交两份报案 -> 归并为一案，重复通知落在可解释状态
- 批改在事故后生效 -> 不适用于本案，赔付按事故前条款计算
- 追偿回执迟到 -> 补登台账，赔付结果不变，全程可审计
- 断网补传与系统恢复 -> 处置链仍按事故发生顺序整理
- 超出范围的材料不能推进赔付；紧急预付必须记录授权人与上限
"""

import tempfile
import threading
import unittest
from pathlib import Path

from service.desk import Desk, DeskError, EventStore

TERMS = {
    "insured": "示例出口企业",
    "coverages": ["cargo", "political_risk"],
    "regions": ["DE", "US", "BR"],
    "deductible": 10_000,
    "limit": 1_000_000,
    "currency": "USD",
    "notice_days": 30,
    "valid_from": "2026-01-01T00:00:00+00:00",
    "valid_to": "2026-12-31T23:59:59+00:00",
    "timezone": "UTC",
}


def make_desk(**term_overrides) -> Desk:
    desk = Desk(EventStore())
    terms = {**TERMS, **term_overrides}
    desk.register_policy(actor="ops", terms=terms, policy_id="P1")
    return desk


def report(desk: Desk, ref="INC-1", **overrides) -> dict:
    params = dict(
        actor="claims-portal",
        incident_ref=ref,
        policy_id="P1",
        occurred_at="2026-03-01T10:00:00+00:00",
        reported_at="2026-03-02T10:00:00+00:00",
        peril="cargo",
        region="DE",
        handler="理赔员甲",
    )
    params.update(overrides)
    return desk.report_incident(**params)


def notice_codes(view) -> set:
    return {n["code"] for n in view["notices"]}


class FullChainTest(unittest.TestCase):
    """标准链路：报案 -> 回执 -> 调查 -> 材料 -> 核定 -> 预付 -> 赔付。"""

    def test_full_chain_payout_and_audit(self):
        desk = make_desk()
        view = report(desk)
        case_id = view["case_id"]
        self.assertEqual(view["status"], "reported")
        self.assertEqual(view["responsible"], "理赔员甲")
        self.assertEqual(view["next_actions"], ["等待承保人回执"])

        view = desk.record_receipt(case_id, actor="insurer-gw", insurer="INS-A",
                                   ref="ACK-1", received_at="2026-03-03T09:00:00+00:00")
        self.assertEqual(view["status"], "acknowledged")

        view = desk.record_milestone(case_id, actor="surveyor", name="现场查勘",
                                     reached_at="2026-03-05T14:00:00+00:00")
        self.assertEqual(view["status"], "investigating")

        view = desk.submit_material(case_id, actor="理赔员甲", kind="invoice",
                                    summary="商业发票 INV-88", region="DE",
                                    amount=120_000, currency="USD",
                                    submitted_at="2026-03-06T10:00:00+00:00")
        self.assertEqual(view["materials"][0]["evaluation"]["result"], "in_scope")

        view = desk.assess_loss(case_id, actor="定损员", amount=120_000,
                                assessor="定损员乙",
                                assessed_at="2026-03-08T10:00:00+00:00")
        self.assertEqual(view["status"], "assessed")
        # 赔付 = min(核定, 保额) - 免赔额 = 120000 - 10000
        self.assertEqual(view["payout"]["payable"], 110_000)
        self.assertEqual(view["payout"]["outstanding"], 110_000)

        view = desk.authorize_prepayment(case_id, actor="理赔员甲", amount=30_000,
                                         authorizer="风控总监", limit=50_000,
                                         reason="客户现金流紧张",
                                         authorized_at="2026-03-09T10:00:00+00:00")
        self.assertEqual(view["status"], "prepaid")
        self.assertEqual(view["payout"]["outstanding"], 80_000)
        auth = view["payout"]["authorizations"][0]
        self.assertEqual((auth["authorizer"], auth["limit"]), ("风控总监", 50_000))

        view = desk.record_payout(case_id, actor="财务", amount=80_000,
                                  paid_at="2026-03-10T10:00:00+00:00")
        self.assertEqual(view["status"], "paid")
        self.assertEqual(view["payout"]["outstanding"], 0)

        # 审计：处置链按业务时间整理，金额/责任人/下一步动作可复核
        chain = desk.case_chain(case_id)
        self.assertEqual([e["type"] for e in chain], [
            "incident_reported", "insurer_receipt", "investigation_milestone",
            "material_submitted", "loss_assessed", "prepayment_authorized",
            "payout_made",
        ])
        business_times = [e["business_time"] for e in chain]
        self.assertEqual(business_times, sorted(business_times))
        self.assertTrue(all(e["actor"] for e in chain))

    def test_payout_capped_by_policy_limit(self):
        desk = make_desk(limit=100_000)
        case_id = report(desk)["case_id"]
        view = desk.assess_loss(case_id, actor="定损员", amount=500_000,
                                assessor="定损员乙")
        self.assertEqual(view["payout"]["covered_loss"], 100_000)
        self.assertEqual(view["payout"]["payable"], 90_000)

    def test_overpayment_rejected_and_logged(self):
        desk = make_desk()
        case_id = report(desk)["case_id"]
        desk.assess_loss(case_id, actor="定损员", amount=50_000, assessor="乙")
        with self.assertRaises(DeskError) as ctx:
            desk.record_payout(case_id, actor="财务", amount=40_001)
        self.assertIn("超出应付余额", str(ctx.exception))
        types = [e["type"] for e in desk.case_chain(case_id)]
        self.assertIn("payout_rejected", types)


class ConcurrentDuplicateReportTest(unittest.TestCase):
    """业务复盘场景一：并发提交两份报案。"""

    def test_concurrent_reports_merge_into_one_case(self):
        desk = make_desk()
        barrier = threading.Barrier(2)
        results, errors = {}, {}

        def report_once(tag, portal):
            try:
                barrier.wait(timeout=5)
                results[tag] = report(desk, actor=portal, portal=portal)
            except Exception as exc:  # pragma: no cover
                errors[tag] = exc

        threads = [threading.Thread(target=report_once, args=(tag, portal))
                   for tag, portal in (("a", "承保人A门户"), ("b", "承保人B门户"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertFalse(errors)

        # 只立一案，两次报案拿到同一案件
        self.assertEqual(results["a"]["case_id"], results["b"]["case_id"])
        case_id = results["a"]["case_id"]
        self.assertEqual(len(desk.list_cases()), 1)

        # 重复通知落在可解释状态，处置链留痕
        view = desk.case_view(case_id)
        self.assertIn("duplicate_notice", notice_codes(view))
        self.assertEqual(view["duplicate_reports"], 1)
        types = [e["type"] for e in desk.case_chain(case_id)]
        self.assertEqual(types.count("incident_reported"), 1)
        self.assertEqual(types.count("duplicate_notice"), 1)


class EndorsementTest(unittest.TestCase):
    """业务复盘场景二：批改进在事故后生效。"""

    def test_endorsement_after_incident_not_applicable(self):
        desk = make_desk()
        case_id = report(desk)["case_id"]
        # 批改：免赔额 10000 -> 1000，生效时间晚于事故时间
        desk.record_endorsement("P1", actor="核保", changes={"deductible": 1_000},
                                effective_at="2026-03-05T00:00:00+00:00",
                                note="年中续约优惠")
        view = desk.assess_loss(case_id, actor="定损员", amount=100_000,
                                assessor="乙")
        # 仍按事故前条款：100000 - 10000
        self.assertEqual(view["payout"]["deductible"], 10_000)
        self.assertEqual(view["payout"]["payable"], 90_000)
        self.assertIn("endorsement_not_applicable", notice_codes(view))

    def test_endorsement_before_incident_applies(self):
        desk = make_desk()
        desk.record_endorsement("P1", actor="核保", changes={"deductible": 1_000},
                                effective_at="2026-02-01T00:00:00+00:00",
                                note="年初续约优惠")
        case_id = report(desk)["case_id"]
        view = desk.assess_loss(case_id, actor="定损员", amount=100_000,
                                assessor="乙")
        self.assertEqual(view["payout"]["deductible"], 1_000)
        self.assertEqual(view["payout"]["payable"], 99_000)
        self.assertIn("endorsement_applied", notice_codes(view))

    def test_endorsement_widening_region_after_incident_does_not_rescue(self):
        desk = make_desk()
        case_id = report(desk, region="FR")["case_id"]  # FR 不在承保范围
        desk.record_endorsement("P1", actor="核保",
                                changes={"regions": ["DE", "US", "BR", "FR"]},
                                effective_at="2026-03-10T00:00:00+00:00")
        view = desk.case_view(case_id)
        self.assertEqual(view["status"], "out_of_scope")
        self.assertIn("incident_out_of_scope", notice_codes(view))
        self.assertIn("endorsement_not_applicable", notice_codes(view))


class LateSubrogationTest(unittest.TestCase):
    """业务复盘场景三：追偿回执迟到。"""

    def test_late_subrogation_receipt_recorded_after_settlement(self):
        desk = make_desk()
        case_id = report(desk)["case_id"]
        desk.assess_loss(case_id, actor="定损员", amount=60_000, assessor="乙",
                         assessed_at="2026-03-05T10:00:00+00:00")
        view = desk.record_payout(case_id, actor="财务", amount=50_000,
                                  paid_at="2026-03-06T10:00:00+00:00")
        self.assertEqual(view["status"], "paid")

        # 赔付结清后，追偿回执才到
        view = desk.record_subrogation(case_id, actor="追偿岗", amount=20_000,
                                       payer="承运人", ref="REC-7",
                                       received_at="2026-04-01T10:00:00+00:00")
        self.assertEqual(view["status"], "paid")  # 赔付结果不变
        self.assertEqual(view["payout"]["recovered_total"], 20_000)
        self.assertIn("late_subrogation_receipt", notice_codes(view))
        self.assertIn("核对迟到的追偿回执并更新追偿台账", view["next_actions"])

        # 审计：处置链完整，金额与责任人可复核
        chain = desk.case_chain(case_id)
        self.assertEqual(chain[-1]["type"], "subrogation_receipt")
        self.assertEqual(chain[-1]["actor"], "追偿岗")

    def test_early_subrogation_is_not_flagged_late(self):
        desk = make_desk()
        case_id = report(desk)["case_id"]
        desk.assess_loss(case_id, actor="定损员", amount=60_000, assessor="乙",
                         assessed_at="2026-03-05T10:00:00+00:00")
        desk.record_subrogation(case_id, actor="追偿岗", amount=5_000,
                                payer="承运人",
                                received_at="2026-03-05T12:00:00+00:00")
        view = desk.record_payout(case_id, actor="财务", amount=50_000,
                                  paid_at="2026-03-06T10:00:00+00:00")
        self.assertEqual(view["status"], "paid")
        self.assertNotIn("late_subrogation_receipt", notice_codes(view))
        self.assertEqual(view["payout"]["recovered_total"], 5_000)


class OfflineCatchUpAndRecoveryTest(unittest.TestCase):
    """断网补传与系统恢复：处置链仍按事故发生顺序整理。"""

    def test_late_uploaded_events_sorted_by_business_time(self):
        desk = make_desk()
        case_id = report(desk)["case_id"]
        # 先补传较晚的调查节点，再补传较早的回执（断网期间积压）
        desk.record_milestone(case_id, actor="查勘员", name="现场查勘",
                              reached_at="2026-03-04T10:00:00+00:00")
        desk.record_receipt(case_id, actor="insurer-gw", insurer="INS-A",
                            received_at="2026-03-03T10:00:00+00:00")
        types = [e["type"] for e in desk.case_chain(case_id)]
        self.assertEqual(types, ["incident_reported", "insurer_receipt",
                                 "investigation_milestone"])

    def test_recovery_from_jsonl_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.jsonl")
            desk = Desk(EventStore(path))
            desk.register_policy(actor="ops", terms=TERMS, policy_id="P1")
            case_id = report(desk)["case_id"]
            desk.record_milestone(case_id, actor="查勘员", name="现场查勘",
                                  reached_at="2026-03-04T10:00:00+00:00")
            desk.record_receipt(case_id, actor="insurer-gw", insurer="INS-A",
                                received_at="2026-03-03T10:00:00+00:00")
            desk.assess_loss(case_id, actor="定损员", amount=60_000, assessor="乙",
                             assessed_at="2026-03-05T10:00:00+00:00")

            # 模拟系统恢复：从同一存储重建
            recovered = Desk(EventStore(path))
            self.assertEqual([e["type"] for e in recovered.case_chain(case_id)],
                             ["incident_reported", "insurer_receipt",
                              "investigation_milestone", "loss_assessed"])
            view = recovered.case_view(case_id)
            self.assertEqual(view["payout"]["payable"], 50_000)
            # 去重索引同样恢复：恢复后再次收到同一事故报案仍归并
            dup = report(recovered)
            self.assertTrue(dup["deduplicated"])
            self.assertEqual(dup["case_id"], case_id)


class OutOfScopeTest(unittest.TestCase):
    """超出范围的材料不能推进赔付。"""

    def test_out_of_scope_incident_blocked_from_payout(self):
        desk = make_desk()
        case_id = report(desk, region="FR")["case_id"]
        view = desk.case_view(case_id)
        self.assertEqual(view["status"], "out_of_scope")
        self.assertIn("incident_out_of_scope", notice_codes(view))

        # 材料照收但标记超范围，状态不前进
        view = desk.submit_material(case_id, actor="理赔员甲", kind="invoice",
                                    summary="发票 INV-99", region="FR")
        self.assertEqual(view["status"], "out_of_scope")
        self.assertEqual(view["materials"][0]["evaluation"]["result"], "out_of_scope")
        self.assertIn("material_out_of_scope", notice_codes(view))

        # 核定不纳入，预付与赔付均被拒并留痕
        view = desk.assess_loss(case_id, actor="定损员", amount=10_000, assessor="乙")
        self.assertIsNone(view["payout"]["payable"])
        with self.assertRaises(DeskError):
            desk.authorize_prepayment(case_id, actor="理赔员甲", amount=1_000,
                                      authorizer="风控总监", limit=5_000)
        with self.assertRaises(DeskError):
            desk.record_payout(case_id, actor="财务", amount=1_000)
        types = [e["type"] for e in desk.case_chain(case_id)]
        self.assertIn("prepayment_rejected", types)
        self.assertIn("payout_rejected", types)

    def test_out_of_scope_material_on_valid_case_does_not_advance(self):
        desk = make_desk()
        case_id = report(desk)["case_id"]
        view = desk.submit_material(case_id, actor="理赔员甲", kind="invoice",
                                    summary="FR 仓库维修发票", region="FR")
        self.assertEqual(view["materials"][0]["evaluation"]["result"], "out_of_scope")
        self.assertIn("material_out_of_scope", notice_codes(view))
        self.assertEqual(view["status"], "reported")  # 未推进
        self.assertIn("补充保单范围内的有效材料", view["next_actions"])

        view = desk.assess_loss(case_id, actor="定损员", amount=10_000,
                                assessor="乙", currency="EUR")  # 币种不符
        self.assertIsNone(view["payout"]["payable"])
        self.assertIn("assessment_out_of_scope", notice_codes(view))


class PrepaymentTest(unittest.TestCase):
    """紧急预付必须记录授权人和上限。"""

    def test_missing_authorizer_or_limit_rejected_and_logged(self):
        desk = make_desk()
        case_id = report(desk)["case_id"]
        with self.assertRaises(DeskError) as ctx:
            desk.authorize_prepayment(case_id, actor="理赔员甲", amount=1_000,
                                      limit=5_000)
        self.assertIn("授权人", str(ctx.exception))
        with self.assertRaises(DeskError) as ctx:
            desk.authorize_prepayment(case_id, actor="理赔员甲", amount=1_000,
                                      authorizer="风控总监")
        self.assertIn("上限", str(ctx.exception))
        types = [e["type"] for e in desk.case_chain(case_id)]
        self.assertEqual(types.count("prepayment_rejected"), 2)

    def test_amount_above_limit_rejected(self):
        desk = make_desk()
        case_id = report(desk)["case_id"]
        with self.assertRaises(DeskError) as ctx:
            desk.authorize_prepayment(case_id, actor="理赔员甲", amount=60_000,
                                      authorizer="风控总监", limit=50_000)
        self.assertIn("超出授权上限", str(ctx.exception))
        view = desk.case_view(case_id)
        self.assertEqual(view["payout"]["authorizations"], [])
        self.assertEqual(view["rejected_attempts"], 1)


class CrossTimezoneTest(unittest.TestCase):
    """跨时区报案：按绝对时间归一化后再排序与计算通知时限。"""

    def test_cross_timezone_report_normalized(self):
        desk = make_desk(timezone="America/New_York", notice_days=1)
        view = report(
            desk,
            occurred_at="2026-08-01T23:30:00+08:00",   # = 2026-08-01T15:30Z
            reported_at="2026-08-02T23:00:00+08:00",  # = 2026-08-02T15:00Z，未超 1 天
        )
        self.assertIn("cross_timezone_normalized", notice_codes(view))
        self.assertNotIn("late_notice", notice_codes(view))

    def test_notice_deadline_uses_absolute_time(self):
        desk = make_desk(timezone="America/New_York", notice_days=1)
        view = report(
            desk,
            occurred_at="2026-08-01T23:30:00+08:00",   # = 2026-08-01T15:30Z
            reported_at="2026-08-03T00:30:00+08:00",  # = 2026-08-02T16:30Z，超 1 天
        )
        self.assertIn("late_notice", notice_codes(view))
        self.assertIn("向承保人说明迟报原因", view["next_actions"])


class PartialLossTest(unittest.TestCase):
    """部分损失落在可解释状态，案件保持开放。"""

    def test_partial_assessment_keeps_case_open(self):
        desk = make_desk()
        case_id = report(desk)["case_id"]
        view = desk.assess_loss(case_id, actor="定损员", amount=40_000,
                                assessor="乙", partial=True,
                                assessed_at="2026-03-05T10:00:00+00:00")
        self.assertEqual(view["status"], "assessed")
        self.assertIn("partial_loss", notice_codes(view))
        self.assertIn("补充最终损失核定", view["next_actions"])
        # 部分核定期间即使金额已付清也不结案
        view = desk.authorize_prepayment(case_id, actor="理赔员甲", amount=30_000,
                                         authorizer="风控总监", limit=50_000,
                                         authorized_at="2026-03-06T10:00:00+00:00")
        self.assertEqual(view["status"], "prepaid")
        # 最终核定后按最新金额结算
        view = desk.assess_loss(case_id, actor="定损员", amount=70_000,
                                assessor="乙",
                                assessed_at="2026-03-08T10:00:00+00:00")
        self.assertEqual(view["payout"]["payable"], 60_000)
        self.assertEqual(view["payout"]["outstanding"], 30_000)


class MaterialCorrectionTest(unittest.TestCase):
    """状态更正以追加事件表示，原始文件摘要不可替换。"""

    def test_original_summary_immutable_correction_appended(self):
        desk = make_desk()
        case_id = report(desk)["case_id"]
        desk.submit_material(case_id, actor="理赔员甲", kind="bill_of_lading",
                             summary="提单 BL-1（扫描件 10 页）",
                             submitted_at="2026-03-03T10:00:00+00:00")
        material_id = desk.case_view(case_id)["materials"][0]["event_id"]

        with self.assertRaises(DeskError) as ctx:
            desk.correct_material(case_id, actor="理赔员甲",
                                  material_event_id=material_id,
                                  changes={"summary": "提单 BL-1（扫描件 12 页）"},
                                  reason="页数写错了")
        self.assertIn("不可替换", str(ctx.exception))

        view = desk.correct_material(case_id, actor="理赔员甲",
                                     material_event_id=material_id,
                                     changes={"summary_note": "实际为 12 页"},
                                     reason="页数更正")
        self.assertEqual(view["materials"][0]["corrections"][0]["reason"], "页数更正")

        # 原始摘要仍在处置链中，未被改写
        submitted = [e for e in desk.case_chain(case_id)
                     if e["type"] == "material_submitted"]
        self.assertEqual(submitted[0]["payload"]["summary"], "提单 BL-1（扫描件 10 页）")
        types = [e["type"] for e in desk.case_chain(case_id)]
        self.assertIn("material_corrected", types)


class ValidationTest(unittest.TestCase):
    def test_timestamps_must_carry_timezone(self):
        desk = make_desk()
        with self.assertRaises(DeskError) as ctx:
            report(desk, occurred_at="2026-03-01T10:00:00")
        self.assertEqual(ctx.exception.status, 400)

    def test_unknown_case_and_policy_are_404(self):
        desk = make_desk()
        with self.assertRaises(DeskError) as ctx:
            desk.case_view("no-such-case")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(DeskError) as ctx:
            report(desk, policy_id="NOPE")
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
