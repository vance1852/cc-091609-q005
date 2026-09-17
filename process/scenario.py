"""把 ``fixtures/processing_batch.json`` 驱动为平台上的真实业务流。

加载顺序即业务时间线：领料 → 净切加酒润 → 拆批 → 炒制（a-1 补传后签署、
a-2 带缺口签署）→ 干燥/取样/报告 → 偏差登记评估 → a-2 返工为 a-2-r1 →
返工批独立加工取样报告 → 偏差关闭 → 四次放行尝试（前两次必须被拦截）。
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

from .batch import ProcessingPlatform
from .contracts import (
    CurvePoint,
    DeviationKind,
    DeviationStatus,
    ProcessKind,
    ReleaseScope,
    Role,
    Sample,
    SampleStatus,
    TestReport,
)
from .errors import PlatformError
from .specs import SpecificationRegistry, jiuzhi_danggui_spec
from .users import User

ROLE_MAP = {
    "operator": Role.OPERATOR,
    "qa": Role.QA,
    "deviation_owner": Role.DEVIATION_OWNER,
    "rework_planner": Role.REWORK_PLANNER,
    "qp": Role.QP,
}

DISPOSITION_MAP = {
    "rework_approved": DeviationStatus.REWORK_APPROVED,
    "retest_approved": DeviationStatus.RETEST_APPROVED,
    "concession_approved": DeviationStatus.CONCESSION_APPROVED,
    "rejected": DeviationStatus.REJECTED,
}

KIND_MAP = {
    "clean": ProcessKind.CLEAN,
    "cut": ProcessKind.CUT,
    "add_excipient": ProcessKind.ADD_EXCIPIENT,
    "moisten": ProcessKind.MOISTEN,
    "fry": ProcessKind.FRY,
    "dry": ProcessKind.DRY,
    "sample": ProcessKind.SAMPLE,
    "pack": ProcessKind.PACK,
}

DEVKIND_MAP = {
    "temperature-gap": DeviationKind.TEMPERATURE_GAP,
    "yield-mismatch": DeviationKind.YIELD_MISMATCH,
    "parameter-excursion": DeviationKind.PARAMETER_EXCURSION,
    "other": DeviationKind.OTHER,
}


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _curve(points: list[list]) -> tuple[CurvePoint, ...]:
    return tuple(CurvePoint(int(t), Decimal(str(v))) for t, v in points)


@dataclass
class ScenarioResult:
    platform: ProcessingPlatform
    users: dict[str, User]
    attempts: list[dict] = field(default_factory=list)


def build_registry() -> SpecificationRegistry:
    registry = SpecificationRegistry()
    registry.register(jiuzhi_danggui_spec("v2024.1"))
    return registry


def load_fixture(path: str | Path | None = None) -> tuple[dict, ScenarioResult]:
    path = Path(path) if path else Path(__file__).parent.parent / "fixtures" / "processing_batch.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    registry = build_registry()
    pf = ProcessingPlatform(registry)
    users = {
        u["id"]: User(u["id"], u["name"], tuple(ROLE_MAP[r] for r in u["roles"]))
        for u in data["users"]
    }
    op = users["op-zhao"]
    qa = users["qa-sun"]
    dev_owner = users["dev-qian"]
    planner = users["plan-li"]
    qp = users["qp-wang"]

    processes = {p["recordId"]: p for p in data["processes"]}
    curves = data["curves"]

    # --- 历史批 proc-0914-z：只建账并直接登记其历史样品/报告 -------------
    for m in data["materials"]:
        pf.issue(op, m["lot"], m["product"], Decimal(m["initialKg"]), _dt(m["at"]),
                 is_excipient=m.get("isExcipient", False))
    hist_sample = Sample(
        sample_id="s-0914", lot_id="proc-0914-z", taken_from_record="-",
        quantity_kg=Decimal("0.3"), drawn_by="qa-prev", drawn_at=_dt("2026-09-14T15:00"),
        status=SampleStatus.TESTED, report_id="r-0914",
    )
    pf.samples["s-0914"] = hist_sample

    def run_process(p: dict) -> None:
        """执行一条非加酒工序（加酒走 add_excipient）。"""
        curve = _curve(curves[p["curve"]]["initialPoints"]) if p.get("curve") else ()
        return pf.record_process(
            op,
            record_id=p["recordId"],
            lot_id=p["lot"],
            kind=KIND_MAP[p["kind"]],
            version=p["version"],
            started_at=_dt(p["start"]),
            ended_at=_dt(p["end"]),
            parameters=p["params"],
            input_kg=Decimal(p["inputKg"]) if "inputKg" in p else None,
            output_kg=Decimal(p["outputKg"]) if "outputKg" in p else None,
            curve=curve,
            at=_dt(p["end"]),
        )

    # --- 阶段 A：拆批前的共同前段 --------------------------------------
    front = ["rec-clean", "rec-cut"]
    for rid in front:
        run_process(processes[rid])
        pf.confirm_record(op, rid, _dt(processes[rid]["end"]))

    addwine = processes["rec-addwine"]
    pf.add_excipient(
        op, "rec-addwine", addwine["lot"], addwine["excipientLot"],
        Decimal(addwine["excipientKg"]), addwine["version"],
        _dt(addwine["start"]), _dt(addwine["end"]), addwine["params"],
    )
    pf.confirm_record(op, "rec-addwine", _dt(addwine["end"]))
    run_process(processes["rec-moisten"])
    pf.confirm_record(op, "rec-moisten", _dt(processes["rec-moisten"]["end"]))

    # --- 阶段 B：拆批 a-1 / a-2 ----------------------------------------
    split = next(m for m in data["movements"] if m["kind"] == "split")
    pf.split_lot(
        op, split["source"],
        {k: Decimal(v) for k, v in split["amounts"].items()},
        Decimal(split["lossKg"]), _dt(split["at"]),
    )

    # --- 阶段 C：a-1 炒制——未签署记录靠传感器补传填补缺口 ---------------
    fry_a1 = processes["rec-fry-a1"]
    pf.record_process(
        op, "rec-fry-a1", "a-1", ProcessKind.FRY, fry_a1["version"],
        _dt(fry_a1["start"]), _dt(fry_a1["end"]), fry_a1["params"],
        input_kg=Decimal(fry_a1["inputKg"]), output_kg=Decimal(fry_a1["outputKg"]),
        curve=_curve(curves["curve-fry-a1"]["initialPoints"]),
        at=_dt(fry_a1["end"]),
    )
    bf = fry_a1["backfill"]
    pf.backfill_sensor(
        op, "rec-fry-a1", _curve(curves["curve-fry-a1"]["backfillPoints"]),
        bf["digest"], _dt(bf["at"]), tuple(bf["gap"]),
    )
    pf.confirm_record(op, "rec-fry-a1", _dt(fry_a1["confirmedAt"]))

    # a-2 炒制——带约10分钟缺口的曲线被操作人直接签署（问题批）
    fry_a2 = processes["rec-fry-a2"]
    pf.record_process(
        op, "rec-fry-a2", "a-2", ProcessKind.FRY, fry_a2["version"],
        _dt(fry_a2["start"]), _dt(fry_a2["end"]), fry_a2["params"],
        input_kg=Decimal(fry_a2["inputKg"]), output_kg=Decimal(fry_a2["outputKg"]),
        curve=_curve(curves["curve-fry-a2"]["initialPoints"]),
        at=_dt(fry_a2["end"]),
    )
    pf.confirm_record(op, "rec-fry-a2", _dt(fry_a2["confirmedAt"]))

    for rid in ("rec-dry-a1", "rec-dry-a2"):
        run_process(processes[rid])
        pf.confirm_record(op, rid, _dt(processes[rid]["end"]))

    # --- 阶段 D：取样与首轮报告 ----------------------------------------
    for s in data["samples"]:
        if s["sampleId"] == "s-r1":
            continue  # 返工批样品在返工后取
        pf.draw_sample(qa, s["sampleId"], s["lot"], s["fromRecord"],
                       Decimal(s["kg"]), _dt(s["at"]))
    limits = {k: (Decimal(lo), Decimal(hi)) for k, (lo, hi) in data["testLimits"].items()}
    for r in data["reports"]:
        if r["reportId"] in ("r-r1", "r-0914"):
            continue
        pf.issue_report(
            qa, r["reportId"], r["sampleId"],
            {k: Decimal(v) for k, v in r["results"].items()},
            limits, _dt(r["at"]),
        )
    # 历史报告登记
    r0914 = next(r for r in data["reports"] if r["reportId"] == "r-0914")
    pf.reports["r-0914"] = TestReport(
        report_id="r-0914", sample_id="s-0914", lot_id="proc-0914-z",
        product="酒炙当归", specification_version="v2024.1",
        issued_at=_dt(r0914["at"]),
        results={k: Decimal(v) for k, v in r0914["results"].items()},
        limits=limits,
    )

    # a-1 包装（在首批放行尝试之前完成）
    run_process(processes["rec-pack-a1"])
    pf.confirm_record(op, "rec-pack-a1", _dt(processes["rec-pack-a1"]["end"]))

    # --- 阶段 E：偏差登记与评估 ----------------------------------------
    devs = {d["deviationId"]: d for d in data["deviations"]}
    dg = devs["dev-gap"]
    pf.open_deviation(
        qa, "dev-gap", dg["lot"], DEVKIND_MAP[dg["kind"]], dg["title"], dg["detail"],
        _dt(dg["openedAt"]), linked_record_id=dg["linkedRecord"],
    )
    pf.assess_deviation(
        dev_owner, "dev-gap", dg["assessment"],
        DISPOSITION_MAP[dg["disposition"]], _dt(dg["assessedAt"]),
    )
    dy = devs["dev-yield"]
    pf.open_deviation(
        qa, "dev-yield", dy["lot"], DEVKIND_MAP[dy["kind"]], dy["title"], dy["detail"],
        _dt(dy["openedAt"]),
    )
    pf.assess_deviation(
        dev_owner, "dev-yield", dy["assessment"],
        DISPOSITION_MAP[dy["disposition"]], _dt(dy["assessedAt"]),
    )

    # --- 阶段 F：返工 a-2 → a-2-r1 -------------------------------------
    rw_move = next(m for m in data["movements"] if m["kind"] == "rework")
    order = pf.plan_rework(planner, "dev-gap", rw_move["target"], _dt(rw_move["at"]))

    for rid in ("rec-fry-r1", "rec-dry-r1", "rec-pack-r1"):
        p = processes[rid]
        curve = _curve(curves[p["curve"]]["initialPoints"]) if p.get("curve") else ()
        pf.record_process(
            op, rid, p["lot"], KIND_MAP[p["kind"]], p["version"],
            _dt(p["start"]), _dt(p["end"]), p["params"],
            input_kg=Decimal(p["inputKg"]), output_kg=Decimal(p["outputKg"]),
            curve=curve, at=_dt(p["end"]),
        )
        pf.confirm_record(op, rid, _dt(p.get("confirmedAt", p["end"])))

    # 返工批补采与新报告
    sr1 = next(s for s in data["samples"] if s["sampleId"] == "s-r1")
    pf.draw_sample(qa, "s-r1", "a-2-r1", sr1["fromRecord"],
                   Decimal(sr1["kg"]), _dt(sr1["at"]))
    rr1 = next(r for r in data["reports"] if r["reportId"] == "r-r1")
    pf.issue_report(
        qa, "r-r1", "s-r1",
        {k: Decimal(v) for k, v in rr1["results"].items()},
        limits, _dt(rr1["at"]),
    )
    pf.complete_rework(planner, order.rework_id, _dt("2026-09-16T18:35:00"))

    # --- 阶段 G：偏差关闭 ----------------------------------------------
    pf.close_deviation(dev_owner, "dev-gap", dg["closureNote"], _dt(dg["closedAt"]))
    pf.close_deviation(dev_owner, "dev-yield", dy["closureNote"], _dt(dy["closedAt"]))

    # --- 阶段 H：四次放行尝试 ------------------------------------------
    attempts: list[dict] = []
    for a in data["releaseAttempts"]:
        user = users[a["decidedBy"]]
        try:
            decision = pf.release(
                user, a["lot"], tuple(a["reports"]), _dt(a["at"]),
                scope=ReleaseScope.FULL, rationale=a["expect"],
            )
            attempts.append({"attempt": a, "decision": decision, "blocked": False})
        except PlatformError as exc:  # 含 ReportBorrowingError / ReleaseBlocked
            attempts.append({"attempt": a, "decision": None,
                             "blocked": True, "reason": str(exc)})

    return data, ScenarioResult(platform=pf, users=users, attempts=attempts)
