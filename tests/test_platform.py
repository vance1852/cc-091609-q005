"""平台规则测试（unittest，无第三方依赖）。

运行：``python -m unittest discover -s tests -v``
"""

import unittest
from datetime import datetime
from decimal import Decimal

from process.batch import ProcessingPlatform
from process.contracts import (
    CurvePoint,
    DeviationKind,
    DeviationStatus,
    MaterialMovement,
    MovementKind,
    ProcessKind,
    ReleaseScope,
    Role,
)
from process.errors import (
    AuthorizationError,
    ConservationError,
    DeviationStateError,
    RecordStateError,
    ReleaseBlocked,
    ReportBorrowingError,
    SpecificationError,
)
from process.scenario import load_fixture
from process.specs import (
    SpecificationRegistry,
    jiuzhi_danggui_spec,
)
from process.users import User

D = Decimal


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def curve_full(points=21, *, temp_offset=0, start=0, step=30):
    return tuple(
        CurvePoint(start + i * step, D(str(150 + temp_offset)))
        for i in range(points)
    )


LIMITS = {
    "水分": (D("8.0"), D("12.0")),
    "总灰分": (D("0"), D("7.0")),
    "浸出物": (D("45.0"), D("100.0")),
}
RESULTS = {"水分": D("10.0"), "总灰分": D("5.0"), "浸出物": D("58.0")}


def make_users():
    return {
        "op": User("op", "操作", (Role.OPERATOR,)),
        "qa": User("qa", "质量", (Role.QA,)),
        "dev": User("dev", "偏差", (Role.DEVIATION_OWNER,)),
        "plan": User("plan", "工艺", (Role.REWORK_PLANNER,)),
        "qp": User("qp", "受权人", (Role.QP,)),
    }


def make_platform() -> ProcessingPlatform:
    registry = SpecificationRegistry()
    registry.register(jiuzhi_danggui_spec("v2024.1"))
    return ProcessingPlatform(registry)


def build_front(pf, u, lot="L", *, wine="W", wine_kg="12", herb_kg="100"):
    """建立净制→切制→加酒→润制的共同前段并全部签署，返回时间点。"""
    t = dt("2026-09-15T07:00")
    pf.issue(u["op"], lot, "酒炙当归", D(herb_kg), t)
    pf.issue(u["op"], wine, "黄酒", D(wine_kg), t, is_excipient=True)
    pf.record_process(u["op"], f"{lot}-clean", lot, ProcessKind.CLEAN, "v2024.1",
                      t, t, {"remove_impurity_pct": "1"}, at=t)
    pf.confirm_record(u["op"], f"{lot}-clean", t)
    pf.record_process(u["op"], f"{lot}-cut", lot, ProcessKind.CUT, "v2024.1",
                      t, t, {"slice_thickness_mm": "1.5"}, at=t)
    pf.confirm_record(u["op"], f"{lot}-cut", t)
    pf.add_excipient(
        u["op"], f"{lot}-add", lot, wine, D(wine_kg), "v2024.1", t, t,
        {"wine_ratio_pct": "12", "wine_alcohol_degree": "14"},
    )
    pf.confirm_record(u["op"], f"{lot}-add", t)
    pf.record_process(u["op"], f"{lot}-moisten", lot, ProcessKind.MOISTEN,
                      "v2024.1", t, t,
                      {"moisten_minutes": "45", "turning_count": "3"}, at=t)
    pf.confirm_record(u["op"], f"{lot}-moisten", t)
    return t


class ConservationTests(unittest.TestCase):
    def setUp(self):
        self.pf = make_platform()
        self.u = make_users()

    def test_split_must_balance_to_the_gram(self):
        pf, u = self.pf, self.u
        pf.issue(u["op"], "L", "酒炙当归", D("100"), dt("2026-09-15T07:00"))
        pf.ledger.open_lot("x1", "酒炙当归", D("0"), created_at=dt("2026-09-15T08:00"))
        pf.ledger.open_lot("x2", "酒炙当归", D("0"), created_at=dt("2026-09-15T08:00"))
        # 产出+损耗 = 60+39 = 99 ≠ 投入 100
        bad = MaterialMovement(
            "m-bad", MovementKind.SPLIT, ("L",), ("x1", "x2"),
            D("100"), D("60"), D("39"), dt("2026-09-15T08:00"),
            target_amounts=(D("60"), D("0")),
        )
        with self.assertRaises(ConservationError):
            pf.ledger.post(bad)
        # 守恒后过账成功：60 + 39 + 1 = 100
        good = MaterialMovement(
            "m-ok", MovementKind.SPLIT, ("L",), ("x1", "x2"),
            D("100"), D("60"), D("40"), dt("2026-09-15T08:01"),
            target_amounts=(D("60"), D("0")),
        )
        pf.ledger.post(good)
        self.assertEqual(pf.ledger.balance("L").balance_kg, D("0.000"))
        self.assertEqual(pf.ledger.balance("x1").balance_kg, D("60.000"))

    def test_overdraw_rejected(self):
        pf, u = self.pf, self.u
        pf.issue(u["op"], "L", "酒炙当归", D("10"), dt("2026-09-15T07:00"))
        pf.ledger.open_lot("y", "酒炙当归", D("0"))
        m = MaterialMovement(
            "m", MovementKind.SPLIT, ("L",), ("y",),
            D("10"), D("10"), D("0"), dt("2026-09-15T08:00"),
            target_amounts=(D("10"),),
        )
        pf.ledger.post(m)
        pf.ledger.open_lot("z", "酒炙当归", D("0"))
        m2 = MaterialMovement(
            "m2", MovementKind.SPLIT, ("L",), ("z",),
            D("1"), D("1"), D("0"), dt("2026-09-15T09:00"),
            target_amounts=(D("1"),),
        )
        with self.assertRaises(ConservationError):
            pf.ledger.post(m2)

    def test_merge_requires_explicit_source_amounts(self):
        pf, u = self.pf, self.u
        pf.issue(u["op"], "L1", "酒炙当归", D("30"), dt("2026-09-15T07:00"))
        pf.issue(u["op"], "L2", "酒炙当归", D("30"), dt("2026-09-15T07:00"))
        pf.ledger.open_lot("M", "酒炙当归", D("0"))
        m = MaterialMovement(
            "m", MovementKind.MERGE, ("L1", "L2"), ("M",),
            D("50"), D("50"), D("0"), dt("2026-09-15T08:00"),
            target_amounts=(D("50"),),
        )
        with self.assertRaises(ConservationError):
            pf.ledger.post(m)  # 多来源未显式给分量

    def test_excipient_addition_is_conserved_and_traceable(self):
        pf, u = self.pf, self.u
        pf.issue(u["op"], "herb", "酒炙当归", D("100"), dt("2026-09-15T07:00"))
        pf.issue(u["op"], "wine", "黄酒", D("12"), dt("2026-09-15T07:00"),
                 is_excipient=True)
        # 加酒前先完成净制、切制（不登记重量移动则结存不变）
        pf.record_process(u["op"], "r-clean", "herb", ProcessKind.CLEAN,
                          "v2024.1", dt("2026-09-15T07:10"),
                          dt("2026-09-15T08:00"),
                          {"remove_impurity_pct": "1"},
                          at=dt("2026-09-15T08:00"))
        pf.record_process(u["op"], "r-cut", "herb", ProcessKind.CUT,
                          "v2024.1", dt("2026-09-15T08:05"),
                          dt("2026-09-15T08:50"),
                          {"slice_thickness_mm": "1.5"},
                          at=dt("2026-09-15T08:50"))
        pf.add_excipient(
            u["op"], "r-add", "herb", "wine", D("12"), "v2024.1",
            dt("2026-09-15T09:00"), dt("2026-09-15T09:10"),
            {"wine_ratio_pct": "12", "wine_alcohol_degree": "14"},
        )
        self.assertEqual(pf.ledger.balance("wine").balance_kg, D("0.000"))
        self.assertEqual(pf.ledger.balance("herb").balance_kg, D("112.000"))


class SpecificationTests(unittest.TestCase):
    def setUp(self):
        self.pf = make_platform()
        self.u = make_users()

    def test_parameter_out_of_range_rejected(self):
        pf, u = self.pf, self.u
        pf.issue(u["op"], "L", "酒炙当归", D("100"), dt("2026-09-15T07:00"))
        with self.assertRaises(SpecificationError):
            pf.record_process(
                u["op"], "r1", "L", ProcessKind.FRY, "v2024.1",
                dt("2026-09-15T10:00"), dt("2026-09-15T10:12"),
                {"set_temp_c": "220", "fry_minutes": "10"},  # 超文火上限
                at=dt("2026-09-15T10:12"),
            )

    def test_curve_temperature_out_of_window_rejected(self):
        pf, u = self.pf, self.u
        pf.issue(u["op"], "L", "酒炙当归", D("100"), dt("2026-09-15T07:00"))
        with self.assertRaises(SpecificationError):
            pf.record_process(
                u["op"], "r1", "L", ProcessKind.FRY, "v2024.1",
                dt("2026-09-15T10:00"), dt("2026-09-15T10:12"),
                {"set_temp_c": "150", "fry_minutes": "10"},
                curve=curve_full(temp_offset=60),  # 210℃ 超窗
                at=dt("2026-09-15T10:12"),
            )

    def test_retired_or_uneffective_version_rejected(self):
        from dataclasses import replace as dc_replace

        registry = SpecificationRegistry()
        registry.register(jiuzhi_danggui_spec("v2024.1"))
        pf = ProcessingPlatform(registry)
        u = make_users()
        pf.issue(u["op"], "L", "酒炙当归", D("100"), dt("2026-09-15T07:00"))

        # 引用已废止版本
        registry.register(jiuzhi_danggui_spec("v2022.0"))
        registry.retire("酒炙当归", "v2022.0")
        with self.assertRaises(SpecificationError):
            pf.record_process(
                u["op"], "r-old", "L", ProcessKind.CLEAN, "v2022.0",
                dt("2026-09-15T07:10"), dt("2026-09-15T08:00"),
                {"remove_impurity_pct": "1"}, at=dt("2026-09-15T08:00"),
            )

        # 引用记录时点尚未生效的未来版本
        future = dc_replace(
            jiuzhi_danggui_spec("v2099.0"),
            effective_from=dt("2099-01-01T00:00"),
        )
        registry.register(future)
        with self.assertRaises(SpecificationError):
            pf.record_process(
                u["op"], "r-future", "L", ProcessKind.CLEAN, "v2099.0",
                dt("2026-09-15T07:10"), dt("2026-09-15T08:00"),
                {"remove_impurity_pct": "1"}, at=dt("2026-09-15T08:00"),
            )

        # 存在更新生效版本时，引用旧版本同样被拒绝
        newer = dc_replace(
            jiuzhi_danggui_spec("v2026.0"),
            effective_from=dt("2026-01-01T00:00"),
        )
        registry.register(newer)
        with self.assertRaises(SpecificationError):
            pf.record_process(
                u["op"], "r-mid", "L", ProcessKind.CLEAN, "v2024.1",
                dt("2026-09-15T07:10"), dt("2026-09-15T08:00"),
                {"remove_impurity_pct": "1"}, at=dt("2026-09-15T08:00"),
            )

        # 只有 v2024.1 生效时，引用它正常
        pf2 = make_platform()
        pf2.issue(u["op"], "L2", "酒炙当归", D("100"), dt("2026-09-15T07:00"))
        pf2.record_process(
            u["op"], "r-ok", "L2", ProcessKind.CLEAN, "v2024.1",
            dt("2026-09-15T07:10"), dt("2026-09-15T08:00"),
            {"remove_impurity_pct": "1"}, at=dt("2026-09-15T08:00"),
        )

    def test_step_order_enforced(self):
        pf, u = self.pf, self.u
        pf.issue(u["op"], "L", "酒炙当归", D("100"), dt("2026-09-15T07:00"))
        # 未净制先切制
        with self.assertRaises(SpecificationError):
            pf.record_process(
                u["op"], "r-cut", "L", ProcessKind.CUT, "v2024.1",
                dt("2026-09-15T08:00"), dt("2026-09-15T08:50"),
                {"slice_thickness_mm": "1.5"}, at=dt("2026-09-15T08:50"),
            )


class CurveAndDeviationTests(unittest.TestCase):
    def setUp(self):
        self.pf = make_platform()
        self.u = make_users()
        # 带 5 分钟缺口（240s -> 540s）的曲线
        self.gappy = tuple(sorted(
            curve_full(9) + (CurvePoint(540, D("150")), CurvePoint(570, D("145")))
        , key=lambda p: p.t_seconds))

    def _fry(self, record_id="r-fry", confirm=False):
        pf, u = self.pf, self.u
        build_front(pf, u)
        pf.record_process(
            u["op"], record_id, "L", ProcessKind.FRY, "v2024.1",
            dt("2026-09-15T10:00"), dt("2026-09-15T10:12"),
            {"set_temp_c": "150", "fry_minutes": "10"},
            curve=self.gappy, at=dt("2026-09-15T10:12"),
        )
        if confirm:
            pf.confirm_record(u["op"], record_id, dt("2026-09-15T10:20"))

    def test_backfill_fills_unsigned_record(self):
        pf, u = self.pf, self.u
        self._fry()
        pf.backfill_sensor(
            u["op"], "r-fry",
            (CurvePoint(270, D("151")), CurvePoint(300, D("150")),
             CurvePoint(330, D("150")), CurvePoint(360, D("150")),
             CurvePoint(390, D("149")), CurvePoint(420, D("149")),
             CurvePoint(450, D("149")), CurvePoint(480, D("148")),
             CurvePoint(510, D("147"))),
            "sha256:bf", dt("2026-09-15T10:15"), (240, 540),
        )
        spec = pf.registry.get("酒炙当归", "v2024.1")
        self.assertEqual(pf._gaps(spec, pf.records["r-fry"]), [])
        pf.confirm_record(u["op"], "r-fry", dt("2026-09-15T10:20"))

    def test_backfill_rejected_after_confirmation(self):
        pf, u = self.pf, self.u
        self._fry(confirm=True)
        with self.assertRaises(RecordStateError):
            pf.backfill_sensor(
                u["op"], "r-fry", (CurvePoint(300, D("150")),),
                "sha256:x", dt("2026-09-15T10:30"), (240, 540),
            )

    def test_confirmed_curve_only_correctable_via_assessed_deviation(self):
        pf, u = self.pf, self.u
        self._fry(confirm=True)
        pf.open_deviation(
            u["qa"], "d1", "L", DeviationKind.TEMPERATURE_GAP, "曲线缺口",
            "240~540s 缺口", dt("2026-09-15T11:00"), linked_record_id="r-fry",
        )
        # 未评估不能修正
        with self.assertRaises(DeviationStateError):
            pf.correct_confirmed_curve(
                u["dev"], "r-fry", "d1", curve_full(), dt("2026-09-15T11:30"),
            )
        pf.assess_deviation(
            u["dev"], "d1", "评估后补曲线", DeviationStatus.RETEST_APPROVED,
            dt("2026-09-15T12:00"),
        )
        pf.correct_confirmed_curve(
            u["dev"], "r-fry", "d1", curve_full(), dt("2026-09-15T12:30"),
        )
        self.assertEqual(pf.records["r-fry"].deviation_id, "d1")
        # 操作人无权做偏差修正
        with self.assertRaises(AuthorizationError):
            pf.correct_confirmed_curve(
                u["op"], "r-fry", "d1", curve_full(), dt("2026-09-15T12:31"),
            )


class RoleSeparationTests(unittest.TestCase):
    def setUp(self):
        self.pf = make_platform()
        self.u = make_users()

    def test_cross_role_actions_denied(self):
        pf, u = self.pf, self.u
        pf.issue(u["op"], "L", "酒炙当归", D("100"), dt("2026-09-15T07:00"))
        # QP 不能执行工序
        with self.assertRaises(AuthorizationError):
            pf.record_process(
                u["qp"], "r1", "L", ProcessKind.CLEAN, "v2024.1",
                dt("2026-09-15T07:10"), dt("2026-09-15T08:00"),
                {"remove_impurity_pct": "1"}, at=dt("2026-09-15T08:00"),
            )
        pf.open_deviation(
            u["qa"], "d1", "L", DeviationKind.OTHER, "x", "x",
            dt("2026-09-15T09:00"),
        )
        # QA 不能评估偏差；操作人也不能
        with self.assertRaises(AuthorizationError):
            pf.assess_deviation(
                u["qa"], "d1", "x", DeviationStatus.REJECTED,
                dt("2026-09-15T09:30"),
            )
        with self.assertRaises(AuthorizationError):
            pf.assess_deviation(
                u["op"], "d1", "x", DeviationStatus.REJECTED,
                dt("2026-09-15T09:30"),
            )
        # 工艺员不能放行
        with self.assertRaises(AuthorizationError):
            pf.release(u["plan"], "L", (), dt("2026-09-15T10:00"))
        # QA 不能放行
        with self.assertRaises(AuthorizationError):
            pf.release(u["qa"], "L", (), dt("2026-09-15T10:00"))


class RetestTests(unittest.TestCase):
    def test_retest_invalidates_old_sample_and_report(self):
        pf = make_platform()
        u = make_users()
        t = dt("2026-09-15T07:00")
        pf.issue(u["op"], "L", "酒炙当归", D("100"), t)
        pf.issue(u["op"], "W", "黄酒", D("12"), t, is_excipient=True)
        for rid, kind, params, curve in [
            ("r-clean", ProcessKind.CLEAN, {"remove_impurity_pct": "1"}, ()),
            ("r-cut", ProcessKind.CUT, {"slice_thickness_mm": "1.5"}, ()),
        ]:
            pf.record_process(u["op"], rid, "L", kind, "v2024.1", t, t,
                              params, at=t, curve=curve)
            pf.confirm_record(u["op"], rid, t)
        pf.add_excipient(
            u["op"], "r-add", "L", "W", D("12"), "v2024.1", t, t,
            {"wine_ratio_pct": "12", "wine_alcohol_degree": "14"},
        )
        pf.confirm_record(u["op"], "r-add", t)
        pf.record_process(u["op"], "r-moisten", "L", ProcessKind.MOISTEN,
                          "v2024.1", t, t,
                          {"moisten_minutes": "45", "turning_count": "3"}, at=t)
        pf.confirm_record(u["op"], "r-moisten", t)
        pf.record_process(u["op"], "r-fry", "L", ProcessKind.FRY, "v2024.1",
                          t, t, {"set_temp_c": "150", "fry_minutes": "10"},
                          curve=curve_full(), at=t)
        pf.confirm_record(u["op"], "r-fry", t)
        pf.record_process(u["op"], "r-dry", "L", ProcessKind.DRY, "v2024.1",
                          t, t, {"dry_temp_c": "70", "moisture_pct": "10"},
                          at=t)
        pf.confirm_record(u["op"], "r-dry", t)
        pf.draw_sample(u["qa"], "s1", "L", "r-dry", D("0.3"), t)
        pf.issue_report(u["qa"], "rep1", "s1", RESULTS, LIMITS, t)
        pf.open_deviation(u["qa"], "d1", "L", DeviationKind.OTHER,
                          "复检", "需复检", t)
        pf.assess_deviation(u["dev"], "d1", "补采",
                            DeviationStatus.RETEST_APPROVED, t)
        pf.retest(u["qa"], "d1", "s2", "r-dry", D("0.3"), "rep2",
                  RESULTS, LIMITS, t)
        self.assertTrue(pf.reports["rep1"].invalidated)
        self.assertEqual(pf.samples["s1"].status.value, "invalidated")
        self.assertTrue(pf.reports["rep2"].is_conforming)
        # 偏差可凭合格复检报告关闭
        pf.close_deviation(u["dev"], "d1", "复检合格关闭", t)
        self.assertEqual(pf.deviations["d1"].status, DeviationStatus.CLOSED)

    def test_retest_requires_approval(self):
        pf = make_platform()
        u = make_users()
        t = dt("2026-09-15T07:00")
        pf.issue(u["op"], "L", "酒炙当归", D("100"), t)
        pf.open_deviation(u["qa"], "d1", "L", DeviationKind.OTHER, "x", "x", t)
        with self.assertRaises(DeviationStateError):
            pf.retest(u["qa"], "d1", "s2", "r-dry", D("0.3"), "rep2",
                      RESULTS, LIMITS, t)


class ReleaseGateTests(unittest.TestCase):
    """用完整批链验证放行闸门的各类拦截。"""

    def _build_full_chain(self, pf, u, lot="L", *, wine="W", confirm=True,
                          curve=None):
        t = dt("2026-09-15T07:00")
        pf.issue(u["op"], lot, "酒炙当归", D("100"), t)
        pf.issue(u["op"], wine, "黄酒", D("12"), t, is_excipient=True)
        steps = [
            ("r-clean", ProcessKind.CLEAN, {"remove_impurity_pct": "1"}, ()),
            ("r-cut", ProcessKind.CUT, {"slice_thickness_mm": "1.5"}, ()),
        ]
        for rid, kind, params, c in steps:
            pf.record_process(u["op"], rid, lot, kind, "v2024.1", t, t,
                              params, at=t, curve=c)
            if confirm:
                pf.confirm_record(u["op"], rid, t)
        pf.add_excipient(
            u["op"], "r-add", lot, wine, D("12"), "v2024.1", t, t,
            {"wine_ratio_pct": "12", "wine_alcohol_degree": "14"},
        )
        if confirm:
            pf.confirm_record(u["op"], "r-add", t)
        pf.record_process(u["op"], "r-moisten", lot, ProcessKind.MOISTEN,
                          "v2024.1", t, t,
                          {"moisten_minutes": "45", "turning_count": "3"}, at=t)
        if confirm:
            pf.confirm_record(u["op"], "r-moisten", t)
        pf.record_process(
            u["op"], "r-fry", lot, ProcessKind.FRY, "v2024.1", t, t,
            {"set_temp_c": "150", "fry_minutes": "10"},
            curve=curve if curve is not None else curve_full(), at=t,
        )
        if confirm:
            pf.confirm_record(u["op"], "r-fry", t)
        pf.record_process(u["op"], "r-dry", lot, ProcessKind.DRY, "v2024.1",
                          t, t, {"dry_temp_c": "70", "moisture_pct": "10"}, at=t)
        if confirm:
            pf.confirm_record(u["op"], "r-dry", t)
        pf.record_process(u["op"], "r-pack", lot, ProcessKind.PACK,
                          "v2024.1", t, t, {"net_weight_g": "500"}, at=t)
        if confirm:
            pf.confirm_record(u["op"], "r-pack", t)
        return t

    def test_release_blocked_without_report(self):
        pf = make_platform()
        u = make_users()
        t = self._build_full_chain(pf, u)
        with self.assertRaises(ReleaseBlocked) as ctx:
            pf.release(u["qp"], "L", (), t)
        self.assertTrue(any("未引用任何检验报告" in f for f in ctx.exception.failures))

    def test_release_blocked_with_unconfirmed_record(self):
        pf = make_platform()
        u = make_users()
        t = self._build_full_chain(pf, u, confirm=False)
        with self.assertRaises(ReleaseBlocked) as ctx:
            pf.release(u["qp"], "L", ("missing",), t)
        self.assertTrue(any("未经操作人签署" in f for f in ctx.exception.failures))

    def test_nonconforming_report_blocks_release(self):
        pf = make_platform()
        u = make_users()
        t = self._build_full_chain(pf, u)
        pf.draw_sample(u["qa"], "s1", "L", "r-dry", D("0.3"), t)
        bad = {"水分": D("13.0"), "总灰分": D("5.0"), "浸出物": D("58.0")}
        pf.issue_report(u["qa"], "rep1", "s1", bad, LIMITS, t)
        with self.assertRaises(ReleaseBlocked) as ctx:
            pf.release(u["qp"], "L", ("rep1",), t)
        self.assertTrue(any("检验结果不合格" in f for f in ctx.exception.failures))

    def test_partial_release_over_balance_rejected(self):
        pf = make_platform()
        u = make_users()
        t = self._build_full_chain(pf, u)
        pf.draw_sample(u["qa"], "s1", "L", "r-dry", D("0.3"), t)
        pf.issue_report(u["qa"], "rep1", "s1", RESULTS, LIMITS, t)
        with self.assertRaises(ReleaseBlocked) as ctx:
            pf.release(u["qp"], "L", ("rep1",), t,
                       scope=ReleaseScope.PARTIAL, quantity_kg=D("1000"))
        self.assertTrue(any("超出成品结存" in f for f in ctx.exception.failures))

    def test_partial_release_within_balance_succeeds(self):
        pf = make_platform()
        u = make_users()
        t = self._build_full_chain(pf, u)
        pf.draw_sample(u["qa"], "s1", "L", "r-dry", D("0.3"), t)
        pf.issue_report(u["qa"], "rep1", "s1", RESULTS, LIMITS, t)
        decision = pf.release(
            u["qp"], "L", ("rep1",), t,
            scope=ReleaseScope.PARTIAL, quantity_kg=D("50"),
        )
        self.assertEqual(decision.scope, ReleaseScope.PARTIAL)
        self.assertEqual(decision.quantity_kg, D("50"))


class ReworkTests(unittest.TestCase):
    def test_rework_lot_links_original_and_needs_new_report(self):
        pf = make_platform()
        u = make_users()
        t = dt("2026-09-15T07:00")
        pf.issue(u["op"], "L", "酒炙当归", D("100"), t)
        pf.issue(u["op"], "W", "黄酒", D("12"), t, is_excipient=True)
        for rid, kind, params in [
            ("r-clean", ProcessKind.CLEAN, {"remove_impurity_pct": "1"}),
            ("r-cut", ProcessKind.CUT, {"slice_thickness_mm": "1.5"}),
        ]:
            pf.record_process(u["op"], rid, "L", kind, "v2024.1", t, t,
                              params, at=t)
            pf.confirm_record(u["op"], rid, t)
        pf.add_excipient(
            u["op"], "r-add", "L", "W", D("12"), "v2024.1", t, t,
            {"wine_ratio_pct": "12", "wine_alcohol_degree": "14"},
        )
        pf.confirm_record(u["op"], "r-add", t)
        pf.record_process(u["op"], "r-moisten", "L", ProcessKind.MOISTEN,
                          "v2024.1", t, t,
                          {"moisten_minutes": "45", "turning_count": "3"}, at=t)
        pf.confirm_record(u["op"], "r-moisten", t)
        pf.record_process(u["op"], "r-fry", "L", ProcessKind.FRY, "v2024.1",
                          t, t, {"set_temp_c": "150", "fry_minutes": "10"},
                          curve=curve_full(), at=t)
        pf.confirm_record(u["op"], "r-fry", t)
        pf.record_process(u["op"], "r-dry", "L", ProcessKind.DRY, "v2024.1",
                          t, t, {"dry_temp_c": "70", "moisture_pct": "10"}, at=t)
        pf.confirm_record(u["op"], "r-dry", t)
        # 原批先取样出报告（返工后原批结存清零，不可能再补取样）
        pf.draw_sample(u["qa"], "s-old", "L", "r-dry", D("0.1"), t)
        pf.issue_report(u["qa"], "r-old", "s-old", RESULTS, LIMITS, t)

        pf.open_deviation(u["qa"], "d1", "L", DeviationKind.OTHER, "返工", "x", t)
        # 未批准返工不能建返工单
        with self.assertRaises(DeviationStateError):
            pf.plan_rework(u["plan"], "d1", "L-r1", t)
        pf.assess_deviation(u["dev"], "d1", "返工",
                            DeviationStatus.REWORK_APPROVED, t)
        order = pf.plan_rework(u["plan"], "d1", "L-r1", t)
        self.assertEqual(pf.ledger.lots["L-r1"].rework_of, "L")
        self.assertEqual(pf.ledger.balance("L-r1").balance_kg, D("111.900"))
        self.assertEqual(pf.ledger.balance("L").balance_kg, D("0.000"))
        # 返工未完成不能关闭偏差；缺工序不能完成返工
        with self.assertRaises(DeviationStateError):
            pf.close_deviation(u["dev"], "d1", "x", t)
        with self.assertRaises(DeviationStateError):
            pf.complete_rework(u["plan"], order.rework_id, t)
        # 返工批重新炒制/干燥/包装
        for rid, kind, params, curve in [
            ("rf-fry", ProcessKind.FRY,
             {"set_temp_c": "150", "fry_minutes": "10"}, curve_full()),
            ("rf-dry", ProcessKind.DRY,
             {"dry_temp_c": "70", "moisture_pct": "10"}, None),
            ("rf-pack", ProcessKind.PACK, {"net_weight_g": "500"}, None),
        ]:
            pf.record_process(u["op"], rid, "L-r1", kind, "v2024.1", t, t,
                              params, at=t, curve=curve or ())
            pf.confirm_record(u["op"], rid, t)
        pf.draw_sample(u["qa"], "s-r1", "L-r1", "rf-dry", D("0.3"), t)
        pf.issue_report(u["qa"], "r-r1", "s-r1", RESULTS, LIMITS, t)
        pf.complete_rework(u["plan"], order.rework_id, t)
        pf.close_deviation(u["dev"], "d1", "返工完成", t)
        # 借用原批旧报告 → 拦截
        with self.assertRaises(ReportBorrowingError):
            pf.release(u["qp"], "L-r1", ("r-old",), t)
        # 本批新报告 → 放行
        decision = pf.release(u["qp"], "L-r1", ("r-r1",), t)
        self.assertEqual(decision.lot_id, "L-r1")
        self.assertEqual(decision.sample_ids, ("s-r1",))


class FixtureScenarioTests(unittest.TestCase):
    def test_fixture_end_to_end(self):
        data, result = load_fixture()
        pf = result.platform
        # 前两次借用报告被拦截
        self.assertTrue(result.attempts[0]["blocked"])
        self.assertTrue(result.attempts[1]["blocked"])
        self.assertIn("proc-0914-z", result.attempts[0]["reason"])
        self.assertIn("a-2", result.attempts[1]["reason"])
        # 后两次成功
        d1, d2 = (a["decision"] for a in result.attempts[2:])
        self.assertEqual((d1.lot_id, d1.quantity_kg), ("a-1", D("70.100")))
        self.assertEqual((d2.lot_id, d2.quantity_kg), ("a-2-r1", D("42.500")))
        # 总平
        self.assertEqual(
            pf.ledger.balance("a-1").balance_kg
            + pf.ledger.balance("a-2-r1").balance_kg
            + D("0.900") + D("20.600"),
            D("120.000") + D("14.100"),
        )
        # 原批与问题批结存清零
        self.assertEqual(pf.ledger.balance("a-2").balance_kg, D("0.000"))
        self.assertEqual(pf.ledger.balance("proc-0915-a").balance_kg, D("0.000"))
        # 偏差均关闭
        for dev in pf.deviations.values():
            self.assertEqual(dev.status, DeviationStatus.CLOSED)
        # 返工关联
        self.assertEqual(pf.ledger.lots["a-2-r1"].rework_of, "a-2")
        # a-1 缺口确实走了补传而非偏差
        self.assertIsNotNone(pf.records["rec-fry-a1"].backfill)
        self.assertIsNone(pf.records["rec-fry-a1"].deviation_id)
        # a-2 缺口挂偏差
        self.assertEqual(pf.records["rec-fry-a2"].deviation_id, "dev-gap")


if __name__ == "__main__":
    unittest.main()
