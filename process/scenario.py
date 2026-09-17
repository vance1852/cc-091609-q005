"""从 fixtures/processing_batch.json 构建完整炮制放行情景。

剧情：

* a-1 分支：炒制曲线出现 3 分钟短时缺口，**未签署前**由传感器补传，正常放行；
* a-2 分支：10 分钟温度缺口在操作人签署后才发现，曲线冻结，只能走偏差单
  由 QA 出具修正记录；收率对不上，经偏差核定核减 0.1 kg；该批转返工，
  新批 r1 关联原批、补采样品、重出新报告；
* 车间曾引用上一批 proc-0914 的旧报告为 a-2 申请放行，被质量受权人拒绝。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .contracts import (
    CurvePoint,
    InspectionReport,
    MaterialLot,
    ProcessKind,
    Role,
    SampleKind,
    TestResult,
    User,
    DispositionKind,
)
from .platform import ProcessingPlatform

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "processing_batch.json"


def _d(value) -> Decimal:
    return Decimal(str(value))


def _curve(start: datetime, minutes: int, *, temp: str = "155",
           step_seconds: int = 30, gap_minutes: int = 0) -> list[CurvePoint]:
    """生成炒制曲线；gap_minutes>0 时在第 2 分钟后制造指定时长的缺口。"""
    points: list[CurvePoint] = []
    end = start + timedelta(minutes=minutes)
    gap_from = start + timedelta(minutes=2)
    gap_to = gap_from + timedelta(minutes=gap_minutes)
    t = start
    while t <= end:
        if not (gap_from <= t < gap_to):
            points.append(CurvePoint(at=t, temp_c=_d(temp)))
        t += timedelta(seconds=step_seconds)
    return points


@dataclass
class Scenario:
    platform: ProcessingPlatform
    data: dict
    ids: dict[str, object] = field(default_factory=dict)
    decisions: dict[str, object] = field(default_factory=dict)


def _results(spec_results: list[dict]) -> tuple[TestResult, ...]:
    return tuple(
        TestResult(
            test_name=r["name"], value=_d(r["value"]),
            lower=_d(r["lower"]) if r["lower"] is not None else None,
            upper=_d(r["upper"]) if r["upper"] is not None else None,
            unit=r["unit"],
        )
        for r in spec_results
    )


def build_scenario(fixture_path: Path | str = FIXTURE) -> Scenario:
    data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    pf = ProcessingPlatform()
    ids: dict[str, object] = {}

    # 人员与角色
    op = pf.register_user(User("u-op", "李操作", Role.OPERATOR))
    gw = pf.register_user(User("u-gw", "采集网关", Role.SENSOR))
    qc = pf.register_user(User("u-qc", "王检验", Role.QC))
    qa = pf.register_user(User("u-qa", "赵质量", Role.QA))
    qp = pf.register_user(User("u-qp", "孙受权", Role.QP))

    base = datetime.fromisoformat(data["worked_at"])
    version = data["specification_version"]
    product = data["product"]

    def clock(minutes: int) -> datetime:
        return base + timedelta(minutes=minutes)

    time = 0

    # ------------------------------------------------ 领料：原料 + 黄酒
    raw = data["raw"]
    pf.ledger.receive(
        MaterialLot(raw["lot"], product, _d(raw["input_kg"])),
        clock(time), note="当归药材领料",
    )
    wine = data["excipient"]
    pf.ledger.receive(
        MaterialLot(wine["lot"], wine["name"], _d(wine["received_kg"])),
        clock(time), note="黄酒领料",
    )

    # ---------------- 净制→润制（拆分前公共工序，记在原料批上）
    main_lot = raw["lot"]
    for stage in data["stages"]:
        kind = ProcessKind(stage["kind"])
        start, end = clock(time), clock(time + 30)
        time += 35
        if kind is ProcessKind.ADD_EXCIPIENT:
            pf.ledger.absorb(main_lot, wine["lot"], _d(wine["added_kg"]), start,
                             note="按 v2023 加入黄酒 12%")
        else:
            pf.ledger.process(main_lot,
                              pf.ledger.balance(main_lot) - _d(stage["loss_kg"]),
                              _d(stage["loss_kg"]), end, note=f"{kind.value} 损耗")
        rec = pf.records.open_record(
            lot_id=main_lot, kind=kind, product=product,
            specification_version=version, started_at=start, ended_at=end,
            parameters={k: _d(v) for k, v in stage.get("params", {}).items()},
            sensor_digest=f"dg-{kind.value}",
        )
        pf.records.confirm(rec.record_id, op, end)

    # ------------------------------------------------ 拆分
    split_at = clock(time)
    split_outputs = {k: _d(v) for k, v in data["split"]["outputs"].items()}
    pf.ledger.split(main_lot, split_outputs, _d(data["split"]["loss_kg"]), split_at)
    time += 10

    branches = data["branches"]

    # ============================ 分支 a-1：缺口在签署前补传
    b1 = branches["proc-0915-a-1"]
    fry_start = clock(time)
    fry_end = fry_start + timedelta(minutes=12)
    sparse = _curve(fry_start, 12, gap_minutes=int(b1["fry"]["sensor_gap_minutes"]))
    r1f = pf.records.open_record(
        lot_id="proc-0915-a-1", kind=ProcessKind.FRY, product=product,
        specification_version=version, started_at=fry_start, ended_at=fry_end,
        parameters={k: _d(v) for k, v in b1["fry"]["params"].items()},
        curve=sparse[:5], sensor_digest="dg-fry-partial",
    )
    # 传感器在操作人签署前补传完整曲线
    full = _curve(fry_start, 12)
    pf.records.backfill(r1f.record_id, gw, full, digest="dg-fry-full")
    pf.ledger.process("proc-0915-a-1",
                      pf.ledger.balance("proc-0915-a-1") - _d(b1["fry"]["loss_kg"]),
                      _d(b1["fry"]["loss_kg"]), fry_end, note="炒制损耗")
    pf.records.confirm(r1f.record_id, op, clock(time + 15))
    time += 20

    d_start, d_end = clock(time), clock(time + 40)
    pf.ledger.process("proc-0915-a-1",
                      pf.ledger.balance("proc-0915-a-1") - _d(b1["dry"]["loss_kg"]),
                      _d(b1["dry"]["loss_kg"]), d_end, note="干燥损耗")
    r1d = pf.records.open_record(
        lot_id="proc-0915-a-1", kind=ProcessKind.DRY, product=product,
        specification_version=version, started_at=d_start, ended_at=d_end,
        parameters={k: _d(v) for k, v in b1["dry"]["params"].items()},
        sensor_digest="dg-dry",
    )
    pf.records.confirm(r1d.record_id, op, d_end)
    time += 45

    s1 = pf.lab.take_sample(qc, "proc-0915-a-1", _d(b1["sample_kg"]),
                            clock(time), kind=SampleKind.ROUTINE, note="成品取样")
    rec_s1 = pf.records.open_record(
        lot_id="proc-0915-a-1", kind=ProcessKind.SAMPLE, product=product,
        specification_version=version, started_at=clock(time), ended_at=clock(time + 5),
        sensor_digest="dg-sample",
    )
    pf.records.confirm(rec_s1.record_id, op, clock(time + 5))
    time += 10
    rep1 = pf.lab.issue_report(
        qc, "proc-0915-a-1", (s1.sample_id,), _results(b1["report_results"]),
        clock(time), specification_version=version,
    )
    time += 10
    pf.ledger.process("proc-0915-a-1", pf.ledger.balance("proc-0915-a-1"),
                      _d(b1["pack_loss_kg"]), clock(time), note="包装")
    r1p = pf.records.open_record(
        lot_id="proc-0915-a-1", kind=ProcessKind.PACK, product=product,
        specification_version=version, started_at=clock(time),
        ended_at=clock(time + 20), sensor_digest="dg-pack",
    )
    pf.records.confirm(r1p.record_id, op, clock(time + 20))
    time += 30

    # ============================ 分支 a-2：签署后缺口 + 收率差异
    b2 = branches["proc-0915-a-2"]
    fry2_start = clock(time)
    fry2_end = fry2_start + timedelta(minutes=12)
    gapped = _curve(fry2_start, 12, gap_minutes=10)
    r2f = pf.records.open_record(
        lot_id="proc-0915-a-2", kind=ProcessKind.FRY, product=product,
        specification_version=version, started_at=fry2_start, ended_at=fry2_end,
        parameters={k: _d(v) for k, v in b2["fry"]["params"].items()},
        curve=gapped, sensor_digest="dg-fry-gap",
    )
    # 操作人在缺口未被发现时即签署——曲线从此冻结
    pf.records.confirm(r2f.record_id, op, fry2_end, require_spec_ok=False)
    pf.ledger.process("proc-0915-a-2",
                      pf.ledger.balance("proc-0915-a-2") - _d(b2["fry"]["loss_kg"]),
                      _d(b2["fry"]["loss_kg"]), fry2_end, note="炒制损耗")
    time += 20

    d2_start, d2_end = clock(time), clock(time + 40)
    pf.ledger.process("proc-0915-a-2",
                      pf.ledger.balance("proc-0915-a-2") - _d(b2["dry"]["loss_kg"]),
                      _d(b2["dry"]["loss_kg"]), d2_end, note="干燥损耗")
    r2d = pf.records.open_record(
        lot_id="proc-0915-a-2", kind=ProcessKind.DRY, product=product,
        specification_version=version, started_at=d2_start, ended_at=d2_end,
        parameters={k: _d(v) for k, v in b2["dry"]["params"].items()},
        sensor_digest="dg-dry",
    )
    pf.records.confirm(r2d.record_id, op, d2_end)
    time += 45

    # 登记上一批旧报告（车间曾试图引用）
    legacy = InspectionReport(
        report_id=data["previous_report"]["report_id"],
        lot_id=data["previous_report"]["lot"],
        sample_ids=("smp-0914-old",), issued_by="u-qc",
        issued_at=base - timedelta(days=1), results=(),
        specification_version=version, external=True,
    )
    pf.lab.reports[legacy.report_id] = legacy

    # 发起两张偏差单
    dev_gap = pf.deviations.raise_deviation(
        op, lot_id="proc-0915-a-2", kind="temperature-gap",
        title="炒制温度曲线 10 分钟缺口", at=clock(time),
        detail="采集网关在 13:22–13:32 离线，操作人已先签署记录",
        impacted_records=(r2f.record_id,),
    )
    time += 5
    on_hand_before = pf.ledger.balance("proc-0915-a-2")
    dev_yield = pf.deviations.raise_deviation(
        op, lot_id="proc-0915-a-2", kind="yield-mismatch",
        title="成品收率与领料量对不上", at=clock(time),
        detail=(f"车间上报 {data['deviations'][1]['reported_kg']} kg，"
                f"台账应为 {on_hand_before} kg"),
    )
    time += 5

    # QP 第一次决定：车间引用上一批旧报告 —— 拒绝
    decision_borrow = pf.reviewer.decide(
        qp, "proc-0915-a-2", clock(time), report_id=legacy.report_id,
    )
    time += 10

    # ---------------- QA 评估：收率差异 → 调账
    pf.deviations.assess(
        qa, dev_yield.deviation_id, DispositionKind.ADJUST, clock(time),
        root_cause="炒制烟损与称量记录漏记，复核批产记录后核定短缺 0.1 kg",
        evidence=("批产记录复核单",),
    )
    time += 5
    pf.ledger.adjust("proc-0915-a-2", _d(b2["adjust_loss_kg"]), clock(time),
                     dev_yield.deviation_id, note="偏差核定核减 0.1 kg")
    pf.deviations.verify(
        qa, dev_yield.deviation_id, clock(time),
        evidence=("台账核减凭证 mv 已复核",),
    )
    pf.deviations.close(qa, dev_yield.deviation_id, clock(time))
    time += 10

    # ---------------- QA 评估：温度缺口 → 修正已签署记录
    pf.deviations.assess(
        qa, dev_gap.deviation_id, DispositionKind.CORRECT, clock(time),
        root_cause="采集网关离线 10 分钟；炒机独立温控导出 CSV 证实温度受控",
        evidence=("炒机温控导出 CSV", "班组情况说明"),
    )
    time += 5
    corrected = pf.records.correct_with_deviation(
        r2f.record_id, qa, dev_gap.deviation_id, clock(time),
        points=_curve(fry2_start, 12, temp="156"),
        digest="dg-fry-corrected-csv",
    )
    pf.deviations.verify(
        qa, dev_gap.deviation_id, clock(time),
        evidence=(f"修正记录 {corrected.record_id} 参数曲线全部合规",),
    )
    pf.deviations.close(qa, dev_gap.deviation_id, clock(time))
    time += 10

    # ---------------- QA 评估：a-2 转返工
    pf.deviations.assess(
        qa, dev_gap.deviation_id, DispositionKind.REWORK, clock(time),
        root_cause="尽管温控证据支持受控，按企业制度该批须返工炒制后重新全检",
        evidence=("质量风险评估表",),
    )
    rw = b2["rework"]
    rw_input = pf.ledger.balance("proc-0915-a-2")
    rw_loss = _d(rw["loss_kg"])
    pf.ledger.rework("proc-0915-a-2", rw["target"], rw_input - rw_loss, rw_loss,
                     clock(time), note="按偏差单返工炒制")
    pf.deviations.verify(
        qa, dev_gap.deviation_id, clock(time),
        evidence=(f"返工批 {rw['target']} 已建立并关联原批",),
    )
    pf.deviations.close(qa, dev_gap.deviation_id, clock(time))
    time += 15

    # ---------------- 返工批 r1：重炒、重干、补采、新报告、包装
    r1lot = rw["target"]
    rf = b2["rework_fry"]
    rf_start, rf_end = clock(time), clock(time) + timedelta(minutes=12)
    pf.ledger.process(r1lot, pf.ledger.balance(r1lot) - _d(rf["loss_kg"]),
                      _d(rf["loss_kg"]), rf_end, note="返工炒制")
    rr = pf.records.open_record(
        lot_id=r1lot, kind=ProcessKind.FRY, product=product,
        specification_version=version, started_at=rf_start, ended_at=rf_end,
        parameters={k: _d(v) for k, v in rf["params"].items()},
        curve=_curve(rf_start, 12, temp="155"), sensor_digest="dg-fry-r1",
    )
    pf.records.confirm(rr.record_id, op, rf_end)
    time += 20

    rd = b2["rework_dry"]
    rd_start, rd_end = clock(time), clock(time + 40)
    pf.ledger.process(r1lot, pf.ledger.balance(r1lot) - _d(rd["loss_kg"]),
                      _d(rd["loss_kg"]), rd_end, note="返干燥")
    rdr = pf.records.open_record(
        lot_id=r1lot, kind=ProcessKind.DRY, product=product,
        specification_version=version, started_at=rd_start, ended_at=rd_end,
        parameters={k: _d(v) for k, v in rd["params"].items()},
        sensor_digest="dg-dry-r1",
    )
    pf.records.confirm(rdr.record_id, op, rd_end)
    time += 45

    ss = pf.lab.take_sample(qc, r1lot, _d(b2["supplement_sample_kg"]),
                            clock(time), kind=SampleKind.SUPPLEMENT,
                            note="返工后补采，不得借用 a-2 留样")
    rs = pf.records.open_record(
        lot_id=r1lot, kind=ProcessKind.SAMPLE, product=product,
        specification_version=version, started_at=clock(time),
        ended_at=clock(time + 5), sensor_digest="dg-sample-r1",
    )
    pf.records.confirm(rs.record_id, op, clock(time + 5))
    time += 10
    rep2 = pf.lab.issue_report(
        qc, r1lot, (ss.sample_id,), _results(b2["report_results"]),
        clock(time), specification_version=version,
    )
    time += 10
    pf.ledger.process(r1lot, pf.ledger.balance(r1lot),
                      _d(b2["pack_loss_kg"]), clock(time), note="返工批包装")
    rp = pf.records.open_record(
        lot_id=r1lot, kind=ProcessKind.PACK, product=product,
        specification_version=version, started_at=clock(time),
        ended_at=clock(time + 20), sensor_digest="dg-pack-r1",
    )
    pf.records.confirm(rp.record_id, op, clock(time + 20))
    time += 30

    # ------------------------------------------------ QP 放行决定
    decision_a1 = pf.reviewer.decide(qp, "proc-0915-a-1", clock(time))
    time += 5
    decision_r1 = pf.reviewer.decide(qp, r1lot, clock(time))
    time += 5
    decision_a2_again = pf.reviewer.decide(qp, "proc-0915-a-2", clock(time))

    ids.update(
        sample_a1=s1.sample_id, report_a1=rep1.report_id,
        sample_r1=ss.sample_id, report_r1=rep2.report_id,
        gapped_record=r2f.record_id, corrected_record=corrected.record_id,
        deviation_gap=dev_gap.deviation_id, deviation_yield=dev_yield.deviation_id,
        rework_lot=r1lot, legacy_report=legacy.report_id,
    )
    scenario = Scenario(pf, data, ids)
    scenario.decisions.update(
        borrow=decision_borrow,
        release_a1=decision_a1,
        release_r1=decision_r1,
        reject_a2=decision_a2_again,
    )
    return scenario
