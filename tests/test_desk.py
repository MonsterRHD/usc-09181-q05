import threading
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile

from service.domain import DeskService, DomainError, EventStore, Projection

UTC = timezone.utc


def t(h, minute=0, day=1, tz=UTC):
    return datetime(2026, 5, day, h, minute, tzinfo=tz).isoformat()


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.svc = DeskService(EventStore(self.path))
        # 保单：货运险，承保东南亚，免赔 500，货运险 48h 报案时限
        self.svc.register_policy(
            "P-1", coverages=["cargo"], regions=["TH", "VN"],
            deductibles={"cargo": "500"}, notice_limits_hours={"cargo": 48},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_duplicate_reports_become_one_claim(self):
        """复盘场景：并发提交两份同一事故报案 -> 主事故 + DUPLICATE，赔付线只有一条。"""
        results, errors = [], []

        def report(incident_id):
            try:
                results.append(self.svc.report_incident(
                    incident_id, policy_id="P-1", loss_type="cargo",
                    occurred_at=t(8, day=10), notified_at=t(10, day=10),
                    reporter="adjuster-A", region="TH", vessel_or_ref="MV-X/0510",
                    client_event_id=f"cli-{incident_id}"))
            except DomainError as e:
                errors.append(e)

        t1 = threading.Thread(target=report, args=("INC-1",))
        t2 = threading.Thread(target=report, args=("INC-2",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(errors, [])
        statuses = {i["incident_id"]: i for i in self.svc.list_incidents()}
        self.assertEqual(statuses["INC-1"]["status"], "PENDING_SCOPE")
        self.assertEqual(statuses["INC-2"]["status"], "DUPLICATE")
        self.assertEqual(statuses["INC-2"]["duplicate_of"], "INC-1")

        # 重复事故不能单独赔付
        with self.assertRaises(DomainError) as cm:
            self.svc.pay("INC-2", "PAY-X", amount="10", paid_at=t(9, day=11))
        self.assertEqual(cm.exception.code, "duplicate_incident")

    def test_offline_retry_is_idempotent(self):
        """断网补传：同一 client_event_id 重传返回首次事件，不产生第二起事故。"""
        kwargs = dict(policy_id="P-1", loss_type="cargo", occurred_at=t(8, day=10),
                      notified_at=t(10, day=10), region="TH", vessel_or_ref="MV-Y/0510",
                      client_event_id="mobile-777")
        first = self.svc.report_incident("INC-9", **kwargs)
        # 设备恢复后用同一幂等键重试（甚至换了本地事故号）
        second = self.svc.report_incident("INC-9-RETRY", **kwargs)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(len(self.svc.list_incidents()), 1)

    def test_endorsement_after_incident_does_not_extend_coverage(self):
        """复盘场景：批改在事故后生效，事故发生时不在范围 -> OUT_OF_SCOPE，赔付冻结。"""
        # 事故 5/10 发生在政治风险（未承保）地区
        self.svc.report_incident(
            "INC-PR", policy_id="P-1", loss_type="political_risk",
            occurred_at=t(8, day=10), notified_at=t(9, day=10), region="XY")
        # 批改 5/12 才生效，加保政治风险与 XY 地区
        self.svc.record_endorsement(
            "P-1", "END-1", effective_from=t(0, day=12),
            coverages_added=["political_risk"], regions_added=["XY"],
            note="事故后加保，不追溯")
        self.svc.decide_scope("INC-PR")
        view = self.svc.get_incident("INC-PR")
        self.assertEqual(view["status"], "OUT_OF_SCOPE")
        self.assertFalse(view["in_scope"])

        # 超出范围：定损与赔付都被拒
        with self.assertRaises(DomainError):
            self.svc.declare_partial_loss("INC-PR", claimed_amount="1000")
        with self.assertRaises(DomainError) as cm:
            self.svc.pay("INC-PR", "PAY-1", amount="100", paid_at=t(8, day=11))
        self.assertEqual(cm.exception.code, "out_of_scope")

    def test_out_of_scope_material_blocks_payment(self):
        inc = self._valid_incident("INC-M")
        # 认可证据可以收
        self.svc.receive_material(inc, "MAT-1", kind="cargo_survey",
                                  summary="原始检验报告：货损 30%", received_at=t(9, day=11))
        # 不被该险种认可的材料：登记留痕但阻断赔付
        self.svc.receive_material(inc, "MAT-2", kind="government_decree",
                                  summary="客户误交的政治法令", received_at=t(9, day=11))
        self.svc.declare_partial_loss(inc, claimed_amount="5000")
        with self.assertRaises(DomainError) as cm:
            self.svc.adjust_loss(inc, adjusted_amount="4000", adjuster="adjuster-A")
        self.assertEqual(cm.exception.code, "blocking_material")

    def test_correction_appends_never_replaces_original_summary(self):
        inc = self._valid_incident("INC-C")
        self.svc.receive_material(inc, "MAT-1", kind="cargo_survey",
                                  summary="原始摘要 V1：货损 30%", received_at=t(9, day=11))
        self.svc.correct_material(inc, "MAT-1", corrected_summary="货损 45%",
                                  reason="复验后更正比例")
        view = self.svc.get_incident(inc)
        mat = view["materials"][0]
        self.assertEqual(mat["summary"], "原始摘要 V1：货损 30%")  # 原摘要不可替换
        self.assertEqual(mat["corrections"][0]["corrected_summary"], "货损 45%")
        self.assertEqual(len(view["corrections"]), 1)

    def test_partial_loss_deductible_advance_and_payment(self):
        inc = self._valid_incident("INC-P")
        self.svc.receive_material(inc, "MAT-1", kind="cargo_survey",
                                  summary="部分损失检验", received_at=t(9, day=11))
        self.svc.declare_partial_loss(inc, claimed_amount="5000", note="部分货损")
        self.assertEqual(self.svc.get_incident(inc)["status"], "PARTIAL_LOSS")

        # 紧急预付必须有授权人；超过授权上限被拒
        with self.assertRaises(DomainError) as cm:
            self.svc.authorize_advance(inc, amount="100", cap="1000",
                                       authorizer="", authorized_at=t(10, day=11))
        self.assertEqual(cm.exception.code, "authorizer_required")
        with self.assertRaises(DomainError) as cm:
            self.svc.authorize_advance(inc, amount="1200", cap="1000",
                                       authorizer="manager-li", authorized_at=t(10, day=11))
        self.assertEqual(cm.exception.code, "advance_cap_exceeded")

        self.svc.authorize_advance(inc, amount="1000", cap="1500",
                                   authorizer="manager-li", authorized_at=t(10, day=11))

        # 定损 4000，免赔 500，预付 1000 -> 可赔尾款 2500
        self.svc.adjust_loss(inc, adjusted_amount="4000", adjuster="adjuster-A")
        view = self.svc.get_incident(inc)
        self.assertEqual(view["deductible_applied"], "500")
        self.assertEqual(view["advances_total"], "1000")

        # 超额赔付被拒（尾款只有 2500）
        with self.assertRaises(DomainError) as cm:
            self.svc.pay(inc, "PAY-1", amount="2600", paid_at=t(8, day=12))
        self.assertEqual(cm.exception.code, "payment_exceeds_payable")
        self.svc.pay(inc, "PAY-1", amount="2500", paid_at=t(8, day=12))
        view = self.svc.get_incident(inc)
        self.assertEqual(view["status"], "PAID")
        self.assertEqual(view["outstanding"], "0")

    def test_late_recovery_receipt_is_auditable(self):
        """复盘场景：追偿回执迟到补录，责任人、金额、下一步动作仍可审计。"""
        inc = self._valid_incident("INC-R")
        self.svc.receive_material(inc, "M", kind="bill_of_lading",
                                  summary="提单", received_at=t(9, day=11))
        self.svc.adjust_loss(inc, adjusted_amount="2000", adjuster="adj-1")
        self.svc.pay(inc, "PAY", amount="1500", paid_at=t(8, day=12))
        self.svc.open_subrogation(inc, responsible_party="carrier-Z",
                                  target_amount="1500", opened_at=t(9, day=12),
                                  owner="recovery-wang")
        # 回执迟到很久才补录
        self.svc.receive_recovery(inc, amount="1500", from_="carrier-Z",
                                  received_at=t(11, day=30), late=True,
                                  reference="SWIFT-9")
        view = self.svc.get_incident(inc)
        self.assertEqual(view["status"], "RECOVERED")
        self.assertTrue(view["recoveries"][0]["late"])
        self.assertEqual(view["subrogation"]["responsible_party"], "carrier-Z")
        self.assertEqual(view["subrogation"]["owner"], "recovery-wang")
        self.assertEqual(view["recovered_total"], "1500")
        self.assertIn("结案", view["next_action"])

    def test_cross_timezone_notices_ordered_by_occurrence(self):
        """跨时区报案：用不同 UTC 偏移提交，列表按事故发生时间（UTC）排序。"""
        # INC-LATE 先发生（曼谷 UTC+7 15:00 == UTC 08:00），INC-EARLY 后发生（纽约 UTC-5 06:00 == UTC 11:00）
        self.svc.report_incident(
            "INC-LATE", policy_id="P-1", loss_type="cargo",
            occurred_at="2026-05-10T15:00:00+07:00",
            notified_at="2026-05-10T16:00:00+07:00", region="TH", vessel_or_ref="V1")
        self.svc.report_incident(
            "INC-EARLY", policy_id="P-1", loss_type="cargo",
            occurred_at="2026-05-10T06:00:00-05:00",
            notified_at="2026-05-10T07:00:00-05:00", region="VN", vessel_or_ref="V2")
        ids = [i["incident_id"] for i in self.svc.list_incidents(order="occurrence")]
        self.assertEqual(ids, ["INC-LATE", "INC-EARLY"])

    def test_notice_late_is_explainable_state(self):
        # 事故 5/10 08:00，报案 5/13（超过 48h）
        self.svc.report_incident(
            "INC-N", policy_id="P-1", loss_type="cargo",
            occurred_at=t(8, day=10), notified_at=t(10, day=13), region="TH",
            vessel_or_ref="V3")
        self.svc.decide_scope("INC-N")
        view = self.svc.get_incident("INC-N")
        self.assertEqual(view["status"], "NOTICE_LATE")
        self.assertFalse(view["within_notice"])
        self.assertIn("48", view["status_reason"])
        self.assertIn("裁量", view["next_action"])

    def test_replay_from_journal_after_restart(self):
        inc = self._valid_incident("INC-Z")
        self.svc.receive_material(inc, "M", kind="damage_photo",
                                  summary="照片", received_at=t(9, day=11))
        self.svc.adjust_loss(inc, adjusted_amount="1000", adjuster="a")
        self.svc.pay(inc, "P", amount="500", paid_at=t(8, day=12))
        # 服务恢复：新 store 重放同一日志，状态一致且仍按事故序
        revived = DeskService(EventStore(self.path))
        view = revived.get_incident(inc)
        self.assertEqual(view["status"], "PAID")
        self.assertEqual(view["paid_total"], "500")
        self.assertEqual([i["incident_id"] for i in revived.list_incidents()], [inc])

    def test_audit_trail_is_append_only_chain(self):
        inc = self._valid_incident("INC-AU")
        self.svc.receive_material(inc, "M", kind="unknown_kind",
                                  summary="杂项", received_at=t(9, day=11))
        trail = self.svc.audit_trail().as_dict()
        # 被拒操作也留在链上
        codes = [e["payload"].get("code") for e in trail["chain"] if e["type"] == "rejected"]
        self.assertIn("material_out_of_scope", codes)
        # seq 单调递增、事件不可变
        seqs = [e["seq"] for e in trail["chain"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    # ---- helpers ----
    def _valid_incident(self, inc_id):
        self.svc.report_incident(
            inc_id, policy_id="P-1", loss_type="cargo",
            occurred_at=t(8, day=10), notified_at=t(9, day=10),
            reporter="r", region="TH", vessel_or_ref=inc_id)
        self.svc.decide_scope(inc_id)
        return inc_id


if __name__ == "__main__":
    unittest.main()
