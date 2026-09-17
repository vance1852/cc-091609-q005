"""物料台账数量守恒与谱系测试。"""

import unittest
from datetime import datetime
from decimal import Decimal

from process.contracts import LotStatus, MaterialLot
from process.errors import ConservationError, LedgerError
from process.ledger import MaterialLedger

T = datetime(2026, 9, 15, 8, 0)


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lg = MaterialLedger()
        self.lg.receive(MaterialLot("raw", "酒炙当归", Decimal("100")), T)

    def test_process_conservation(self):
        self.lg.process("raw", Decimal("90"), Decimal("10"), T)
        self.assertEqual(self.lg.balance("raw"), Decimal("90.000"))
        recon = self.lg.reconciliation()
        self.assertEqual(
            recon["on_hand_kg"] + recon["loss_kg"], recon["received_kg"]
        )

    def test_split_must_clear_whole_lot_and_conserve(self):
        with self.assertRaises(ConservationError):
            self.lg.split("raw", {"a": Decimal("40"), "b": Decimal("40")},
                          Decimal("0"), T)
        self.lg.split("raw", {"a": Decimal("45"), "b": Decimal("45")},
                      Decimal("10"), T)
        self.assertEqual(self.lg.balance("raw"), Decimal("0.000"))
        self.assertEqual(self.lg.balance("a"), Decimal("45.000"))
        info = self.lg.lineage("a")
        self.assertEqual(info["parents"], ["raw"])

    def test_split_gram_mismatch_rejected(self):
        # 120.000 kg 拆成 60.000 + 59.999，差 1 g，必须被拒绝
        with self.assertRaises(ConservationError):
            self.lg.split("raw", {"a": Decimal("60.000"), "b": Decimal("59.999")},
                          Decimal("0"), T)

    def test_merge_conserve(self):
        self.lg.split("raw", {"a": Decimal("50"), "b": Decimal("50")},
                      Decimal("0"), T)
        self.lg.merge(("a", "b"), "c", Decimal("2"), T)
        self.assertEqual(self.lg.balance("c"), Decimal("98.000"))
        self.assertEqual(self.lg.lineage("c")["parents"], ["a", "b"])

    def test_sample_tracked_separately(self):
        self.lg.take_sample("raw", "smp-1", Decimal("0.3"), T)
        self.assertEqual(self.lg.balance("raw"), Decimal("99.700"))
        self.assertEqual(self.lg.sample_balance("smp-1"), Decimal("0.300"))
        recon = self.lg.reconciliation()
        self.assertEqual(
            recon["on_hand_kg"] + recon["loss_kg"] + recon["sampled_kg"],
            recon["received_kg"],
        )

    def test_rework_links_and_quarantines_parent(self):
        self.lg.rework("raw", "raw-r1", Decimal("95"), Decimal("5"), T)
        self.assertEqual(self.lg.lot("raw").status, LotStatus.QUARANTINED)
        self.assertEqual(self.lg.lot("raw-r1").rework_of, "raw")
        self.assertEqual(self.lg.balance("raw-r1"), Decimal("95.000"))
        with self.assertRaises(LedgerError):
            self.lg.process("raw", Decimal("1"), Decimal("0"), T)

    def test_adjust_requires_positive_loss(self):
        with self.assertRaises(ConservationError):
            self.lg.adjust("raw", Decimal("0"), T, "dev-1")
        self.lg.adjust("raw", Decimal("0.1"), T, "dev-1")
        mv = self.lg.movements[-1]
        self.assertEqual(mv.deviation_id, "dev-1")
        self.assertEqual(self.lg.balance("raw"), Decimal("99.900"))

    def test_overdraw_rejected(self):
        with self.assertRaises(LedgerError):
            self.lg.process("raw", Decimal("150"), Decimal("0"), T)


if __name__ == "__main__":
    unittest.main()
