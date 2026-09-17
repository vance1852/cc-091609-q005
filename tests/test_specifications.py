"""工艺版本、参数限度与曲线缺口校验测试。"""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from process.contracts import CurvePoint, ProcessKind
from process.specifications import SpecificationRegistry

T = datetime(2026, 9, 15, 9, 0)


def curve(start: datetime, minutes: int = 12, *, gap: timedelta | None = None,
          temp: str = "155") -> tuple[CurvePoint, ...]:
    points = []
    t = start
    end = start + timedelta(minutes=minutes)
    skip_from = start + timedelta(minutes=2)
    skip_to = skip_from + (gap or timedelta())
    while t <= end:
        if not (skip_from <= t < skip_to):
            points.append(CurvePoint(t, Decimal(temp)))
        t += timedelta(seconds=30)
    return tuple(points)


class SpecificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = SpecificationRegistry()

    def test_effective_version_selection(self):
        self.assertEqual(
            self.reg.effective_version("酒炙当归", datetime(2022, 1, 1)).version,
            "v2020",
        )
        self.assertEqual(
            self.reg.effective_version("酒炙当归", T).version, "v2023"
        )

    def test_unknown_product(self):
        from process.errors import SpecificationError
        with self.assertRaises(SpecificationError):
            self.reg.effective_version("醋香附", T)

    def test_record_using_retired_version_fails(self):
        from process.records import RecordBook
        from process.contracts import ProcessRecord, RecordStatus
        book = RecordBook(self.reg)
        rec = ProcessRecord(
            record_id="x", lot_id="l", kind=ProcessKind.CLEAN,
            product="酒炙当归", specification_version="v2020",
            started_at=T, ended_at=T + timedelta(minutes=10),
            parameters={"foreign_matter_pct": Decimal("1")},
            sensor_digest="d", confirmed_by="u",
            status=RecordStatus.CONFIRMED, signed_at=T,
        )
        problems = self.reg.validate_record(rec)
        self.assertTrue(any("现行版本为 v2023" in p for p in problems), problems)

    def test_parameter_out_of_range(self):
        from process.records import RecordBook
        book = RecordBook(self.reg)
        rec = book.open_record(
            lot_id="l", kind=ProcessKind.FRY, product="酒炙当归",
            specification_version="v2023", started_at=T,
            ended_at=T + timedelta(minutes=12),
            parameters={"fry_minutes": Decimal("40")},
            curve=curve(T),
        )
        problems = self.reg.validate_record(rec)
        self.assertTrue(any("炒制时间" in p for p in problems), problems)

    def test_curve_ten_minute_gap_detected(self):
        from process.records import RecordBook
        book = RecordBook(self.reg)
        rec = book.open_record(
            lot_id="l", kind=ProcessKind.FRY, product="酒炙当归",
            specification_version="v2023", started_at=T,
            ended_at=T + timedelta(minutes=12),
            parameters={"fry_minutes": Decimal("12")},
            curve=curve(T, gap=timedelta(minutes=10)),
        )
        problems = self.reg.validate_record(rec)
        self.assertTrue(any("10 分钟缺口" in p for p in problems), problems)

    def test_continuous_curve_passes(self):
        from process.records import RecordBook
        book = RecordBook(self.reg)
        rec = book.open_record(
            lot_id="l", kind=ProcessKind.FRY, product="酒炙当归",
            specification_version="v2023", started_at=T,
            ended_at=T + timedelta(minutes=12),
            parameters={"fry_minutes": Decimal("12")},
            curve=curve(T),
        )
        self.assertEqual(self.reg.validate_record(rec), [])

    def test_temperature_out_of_band(self):
        from process.records import RecordBook
        book = RecordBook(self.reg)
        rec = book.open_record(
            lot_id="l", kind=ProcessKind.FRY, product="酒炙当归",
            specification_version="v2023", started_at=T,
            ended_at=T + timedelta(minutes=12),
            parameters={"fry_minutes": Decimal("12")},
            curve=curve(T, temp="200"),
        )
        problems = self.reg.validate_record(rec)
        self.assertTrue(any("越限" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
