"""签署/补传/偏差修正、角色权限与返工放行规则测试。"""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from process.contracts import (
    CurvePoint,
    DispositionKind,
    LotStatus,
    MaterialLot,
    ProcessKind,
    Role,
    SampleKind,
    User,
)
from process.errors import RecordError, ReleaseError, WorkflowError
from process.platform import ProcessingPlatform
from process.scenario import build_scenario

T = datetime(2026, 9, 15, 9, 0)


def full_curve(start: datetime, minutes: int = 12, temp: str = "155"):
    return [
        CurvePoint(start + timedelta(seconds=30 * i), Decimal(temp))
        for i in range(minutes * 2 + 1)
    ]


class RecordSigningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pf = ProcessingPlatform()
        self.op = self.pf.register_user(User("op", "操作", Role.OPERATOR))
        self.gw = self.pf.register_user(User("gw", "网关", Role.SENSOR))
        self.qa = self.pf.register_user(User("qa", "质量", Role.QA))

    def _fry(self, with_curve=True):
        return self.pf.records.open_record(
            lot_id="l", kind=ProcessKind.FRY, product="酒炙当归",
            specification_version="v2023", started_at=T,
            ended_at=T + timedelta(minutes=12),
            parameters={"fry_minutes": Decimal("12")},
            curve=full_curve(T) if with_curve else (),
        )

    def test_sensor_can_backfill_only_when_open(self):
        rec = self._fry(with_curve=False)
        self.pf.records.backfill(rec.record_id, self.gw, full_curve(T),
                                 digest="dg")
        signed = self.pf.records.confirm(rec.record_id, self.op,
                                         T + timedelta(minutes=12))
        self.assertEqual(signed.confirmed_by, "op")
        # 签署后补传必须被拒绝
        with self.assertRaises(RecordError):
            self.pf.records.backfill(rec.record_id, self.gw, full_curve(T),
                                     digest="dg2")

    def test_operator_cannot_backfill(self):
        rec = self._fry(with_curve=False)
        with self.assertRaises(RecordError):
            self.pf.records.backfill(rec.record_id, self.op, full_curve(T),
                                     digest="dg")

    def test_confirmed_record_only_fixable_via_deviation(self):
        rec = self._fry()
        self.pf.records.confirm(rec.record_id, self.op,
                                T + timedelta(minutes=12))
        with self.assertRaises(RecordError):
            self.pf.records.confirm(rec.record_id, self.op, T)
        corrected = self.pf.records.correct_with_deviation(
            rec.record_id, self.qa, "dev-9", T + timedelta(hours=1),
            points=full_curve(T, temp="160"), digest="dg-fix",
        )
        self.assertEqual(corrected.supersedes, rec.record_id)
        self.assertEqual(corrected.deviation_id, "dev-9")
        self.assertEqual(
            self.pf.records.records[rec.record_id].status.value, "corrected"
        )

    def test_only_qa_assesses_deviation(self):
        dev = self.pf.deviations.raise_deviation(
            self.op, lot_id="l", kind="x", title="t", at=T)
        with self.assertRaises(WorkflowError):
            self.pf.deviations.assess(
                self.op, dev.deviation_id, DispositionKind.REJECT, T,
                root_cause="r")
        self.pf.deviations.assess(
            self.qa, dev.deviation_id, DispositionKind.USE_AS_IS, T,
            root_cause="r")
        with self.assertRaises(WorkflowError):  # 未验证不能关闭
            self.pf.deviations.close(self.qa, dev.deviation_id, T)
        self.pf.deviations.verify(self.qa, dev.deviation_id, T)
        self.pf.deviations.close(self.qa, dev.deviation_id, T)
        self.assertEqual(self.pf.deviations.get(dev.deviation_id).status.value,
                         "closed")


class ReleaseScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.s = build_scenario()
        cls.pf = cls.s.platform

    def test_global_conservation_to_the_gram(self):
        recon = self.pf.ledger.reconciliation()
        self.assertEqual(
            recon["on_hand_kg"] + recon["loss_kg"] + recon["sampled_kg"],
            recon["received_kg"],
        )
        self.assertEqual(recon["received_kg"], Decimal("135.000"))

    def test_branch_a1_released_after_backfill(self):
        d = self.s.decisions["release_a1"]
        self.assertTrue(d.approved, d.rejected_reasons)
        self.assertEqual(self.pf.ledger.lot("proc-0915-a-1").status,
                         LotStatus.RELEASED)

    def test_borrowed_previous_report_rejected(self):
        d = self.s.decisions["borrow"]
        self.assertFalse(d.approved)
        joined = "\n".join(d.rejected_reasons)
        self.assertIn("rpt-0914-legacy", joined)
        self.assertIn("禁止借用旧报告", joined)

    def test_rework_lot_linked_parent_quarantined(self):
        r1 = "proc-0915-a-2-r1"
        self.assertEqual(self.pf.ledger.lot(r1).rework_of, "proc-0915-a-2")
        self.assertEqual(self.pf.ledger.lot("proc-0915-a-2").status,
                         LotStatus.QUARANTINED)

    def test_rework_lot_released_with_supplement_and_new_report(self):
        d = self.s.decisions["release_r1"]
        self.assertTrue(d.approved, d.rejected_reasons)
        self.assertEqual(d.report_id, self.s.ids["report_r1"])
        self.assertEqual(d.sample_ids, (self.s.ids["sample_r1"],))
        sample = self.pf.lab.samples[self.s.ids["sample_r1"]]
        self.assertEqual(sample.kind, SampleKind.SUPPLEMENT)
        self.assertEqual(sample.lot_id, "proc-0915-a-2-r1")

    def test_original_lot_cannot_be_released_after_rework(self):
        d = self.s.decisions["reject_a2"]
        self.assertFalse(d.approved)
        self.assertIn("隔离状态", "\n".join(d.rejected_reasons))

    def test_gap_record_frozen_and_superseded(self):
        old = self.pf.records.records[self.s.ids["gapped_record"]]
        new = self.pf.records.records[self.s.ids["corrected_record"]]
        self.assertEqual(old.status.value, "corrected")
        self.assertEqual(new.supersedes, old.record_id)
        self.assertEqual(new.deviation_id, self.s.ids["deviation_gap"])

    def test_only_qp_can_release(self):
        qc = self.pf.register_user(User("qc2", "检验2", Role.QC))
        with self.assertRaises(ReleaseError):
            self.pf.reviewer.decide(qc, "proc-0915-a-2-r1", T)

    def test_report_cannot_reference_other_lots_sample(self):
        # 构造一个新批并试图用 r1 的补采样品出报告
        qc = self.pf.user("u-qc")
        with self.assertRaises(WorkflowError):
            self.pf.lab.issue_report(
                qc, "proc-0915-a-1",
                (self.s.ids["sample_r1"],), (), T,
                specification_version="v2023",
            )

    def test_release_scope_matches_ledger_quantity(self):
        d = self.s.decisions["release_r1"]
        self.assertEqual(d.quantity_kg,
                         self.pf.ledger.balance("proc-0915-a-2-r1"))
        self.assertIn("52.800 kg", d.scope)


if __name__ == "__main__":
    unittest.main()
