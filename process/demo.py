"""质量受权人核对视图：``python -m process.demo``。

打开样例即可逐节核对：
A. 每一克物料去向（批号台账回放 + 总平式）
B. 连续批记录（工序顺序、签署、曲线缺口与处理路径）
C. 偏差评估与处置（温度缺口→返工；收率差异→调查更正）
D. 检验报告 ↔ 样品 ↔ 批号引用链（防止借用旧报告）
E. 放行尝试记录与最终放行范围

``python -m process.demo --check`` 额外把 fixtures 中的 expected 台账与
放行结果做机器核对，全部一致时退出码为 0。
"""

import argparse
import sys
from decimal import Decimal

from .contracts import DeviationStatus, ProcessKind, ReleaseScope
from .scenario import load_fixture

KIND_CN = {
    ProcessKind.CLEAN: "净制",
    ProcessKind.CUT: "切制",
    ProcessKind.ADD_EXCIPIENT: "辅料加入",
    ProcessKind.MOISTEN: "润制",
    ProcessKind.FRY: "炒制",
    ProcessKind.DRY: "干燥",
    ProcessKind.SAMPLE: "取样",
    ProcessKind.PACK: "包装",
}

DISP_CN = {
    DeviationStatus.REWORK_APPROVED: "批准返工",
    DeviationStatus.RETEST_APPROVED: "批准补采复检",
    DeviationStatus.CONCESSION_APPROVED: "让步接收",
    DeviationStatus.REJECTED: "判定报废",
    DeviationStatus.CLOSED: "已关闭",
    DeviationStatus.OPEN: "待评估",
}


def _line(title: str) -> str:
    return f"\n{'='*72}\n{title}\n{'='*72}"


def render(data: dict, result) -> str:
    pf = result.platform
    out: list[str] = []
    exp = data["expected"]

    # ------------------------------------------------------------------
    # 封面
    # ------------------------------------------------------------------
    out.append(_line(f"炮制批次与质量放行核对视图 — {data['product']}"))
    out.append(data["description"])

    # ------------------------------------------------------------------
    # A. 逐克物料核对
    # ------------------------------------------------------------------
    out.append(_line("A. 物料台账：每一克去向"))
    out.append(f"{'批号':<14}{'建账':>10}{'转入':>10}{'转出':>10}{'损耗':>10}{'增重':>10}{'结存':>10}")
    for lot in ("proc-0915-a", "wine-0915", "a-1", "a-2", "a-2-r1"):
        b = pf.ledger.balance(lot)
        lot_obj = pf.ledger.lots[lot]
        tag = "（辅料）" if lot_obj.is_excipient else ""
        tag += f"（返工自 {lot_obj.rework_of}）" if lot_obj.rework_of else ""
        out.append(
            f"{lot:<14}{lot_obj.initial_kg:>10}{b.total_in_kg:>10}{b.total_out_kg:>10}"
            f"{b.loss_kg:>10}{b.gain_kg:>10}{b.balance_kg:>10}  {tag}"
        )

    out.append("")
    out.append("成品批 a-1 台账回放：")
    for row in pf.ledger.trace("a-1"):
        note = row.get("note", "")
        out.append(
            f"  {row['movement_id']:<16} 转入 {row['in_kg']:>8}  转出 {row['out_kg']:>8}  "
            f"损耗 {row['loss_kg']:>8}  {note}"
        )
    out.append("")
    out.append(f"总平式（fixture 预期）：{exp['reconciliation']}")
    out.append(
        "实际：领料药材 120.000 + 黄酒转入 14.100 = "
        f"成品 {exp['finishedKg']}（a-1 {exp['finishedByLot']['a-1']} + "
        f"a-2-r1 {exp['finishedByLot']['a-2-r1']}）+ 取样 {exp['sampleKg']} + "
        f"工序净损耗 {exp['processLossKg']}"
    )

    # ------------------------------------------------------------------
    # B. 连续批记录
    # ------------------------------------------------------------------
    out.append(_line("B. 连续批记录（沿物料谱系追溯）"))
    for terminal in ("a-1", "a-2-r1"):
        out.append(f"\n— 成品批 {terminal} 的连续批记录 —")
        for r in pf.record_chain(terminal):
            spec = pf.registry.get(r.product, r.specification_version)
            gaps = pf._gaps(spec, r)  # noqa: SLF001
            curve_note = ""
            if r.kind == ProcessKind.FRY:
                if gaps:
                    cover = [d.deviation_id for d in pf._record_deviations(r.record_id)]  # noqa: SLF001
                    curve_note = f"曲线缺口 {len(gaps)} 处 → 偏差 {cover or '未覆盖！'}"
                elif r.backfill:
                    curve_note = (
                        f"曲线完整（{r.backfill.received_at:%H:%M} 传感器补传"
                        f" {r.backfill.gap_seconds[0]}~{r.backfill.gap_seconds[1]}s "
                        f"后签署）"
                    )
                else:
                    curve_note = "曲线完整（一次采集）"
            dev_note = f"，挂偏差 {r.deviation_id}" if r.deviation_id else ""
            out.append(
                f"  [{r.order_no:>2}] {KIND_CN[r.kind]:<4} {r.record_id:<12} "
                f"{r.started_at:%m-%d %H:%M} 版本 {r.specification_version} "
                f"签署人 {r.confirmed_by or '未签署'}{dev_note} {curve_note}"
            )

    # ------------------------------------------------------------------
    # C. 偏差
    # ------------------------------------------------------------------
    out.append(_line("C. 偏差评估与处置（不同角色推进）"))
    for dev in pf.deviations.values():
        out.append(
            f"\n[{dev.deviation_id}] {dev.title}\n"
            f"  批号 {dev.lot_id}｜登记 {dev.opened_by} {dev.opened_at:%m-%d %H:%M}\n"
            f"  评估 {dev.assessed_by}：{DISP_CN.get(dev.disposition, dev.disposition)}"
            f"（{dev.assessed_at:%m-%d %H:%M}）\n"
            f"  调查：{dev.assessment}\n"
            f"  关联记录：{dev.linked_record_id or '-'}｜关联报告：{dev.linked_report_id or '-'}\n"
            f"  状态：{DISP_CN[dev.status]}（关闭人 {dev.closed_by}）\n"
            f"  关闭说明：{dev.closure_note}"
        )

    # ------------------------------------------------------------------
    # D. 报告-样品-批号
    # ------------------------------------------------------------------
    out.append(_line("D. 检验报告 ↔ 样品 ↔ 批号引用链"))
    out.append(f"{'报告':<8}{'样品':<8}{'批号':<13}{'结论':<6}{'状态':<10}所跟工序记录")
    for rid in ("r-0914", "r-a1", "r-a2", "r-r1"):
        r = pf.reports[rid]
        s = pf.samples.get(r.sample_id)
        verdict = "合格" if r.is_conforming else "不合格"
        state = "已作废" if r.invalidated else "有效"
        from_rec = s.taken_from_record if s else "-"
        out.append(
            f"{rid:<8}{r.sample_id:<8}{r.lot_id:<13}{verdict:<6}{state:<10}{from_rec}"
            f"{'（替代 ' + r.supersedes + '）' if r.supersedes else ''}"
        )
    out.append("")
    out.append("规则：报告只对其样品与批号负责；返工批必须凭本批新样品/新报告放行。")

    # ------------------------------------------------------------------
    # E. 放行
    # ------------------------------------------------------------------
    out.append(_line("E. 放行尝试与最终放行范围（仅质量受权人 qp-wang）"))
    for a in result.attempts:
        req = a["attempt"]
        if a["blocked"]:
            out.append(f"\n✗ 拦截：{req['lot']} 引用 {req['reports']}（{req['at']}）")
            out.append(f"  原因：{a['reason']}")
        else:
            d = a["decision"]
            out.append(
                f"\n✓ 放行决定 {d.decision_id}：批号 {d.lot_id}，范围 {d.scope.value}，"
                f"数量 {d.quantity_kg} kg"
            )
            out.append(
                f"  报告 {list(d.report_ids)} ← 样品 {list(d.sample_ids)}；"
                f"谱系内偏差 {list(d.deviation_ids)} 均已关闭"
            )
    out.append("")
    out.append(
        f"最终放行范围：a-1 {exp['finalRelease'][0]['kg']} kg + "
        f"a-2-r1 {exp['finalRelease'][1]['kg']} kg = {exp['finishedKg']} kg；"
        f"原批 a-2 不予放行（已返工，结存 0.000 kg）。"
    )
    return "\n".join(out)


def check(data: dict, result) -> list[str]:
    """把 fixture 的 expected 段与平台实际状态核对，返回不一致项列表。"""
    pf = result.platform
    exp = data["expected"]
    problems: list[str] = []

    def expect(label: str, actual: Decimal, want: str) -> None:
        if Decimal(actual) != Decimal(want):
            problems.append(f"{label}: 实际 {actual} ≠ 预期 {want}")

    expect("黄酒批结存", pf.ledger.balance("wine-0915").balance_kg, exp["wineLotBalanceKg"])
    expect("a-1 成品", pf.ledger.balance("a-1").balance_kg, exp["finishedByLot"]["a-1"])
    expect("a-2-r1 成品", pf.ledger.balance("a-2-r1").balance_kg, exp["finishedByLot"]["a-2-r1"])
    expect("a-2 结存（应清零）", pf.ledger.balance("a-2").balance_kg, "0.000")
    expect("原批结存", pf.ledger.balance("proc-0915-a").balance_kg, "0.000")
    finished = pf.ledger.balance("a-1").balance_kg + pf.ledger.balance("a-2-r1").balance_kg
    expect("成品合计", finished, exp["finishedKg"])

    # 放行尝试：前两次必须拦截，后两次必须成功且范围/数量正确
    assert len(result.attempts) == 4
    if not result.attempts[0]["blocked"] or not result.attempts[1]["blocked"]:
        problems.append("借用报告的放行尝试未被拦截")
    released = [a for a in result.attempts[2:] if not a["blocked"]]
    if len(released) != 2:
        problems.append("应有两批整批放行成功")
    else:
        for a, want in zip(released, exp["finalRelease"]):
            d = a["decision"]
            if d.lot_id != want["lot"]:
                problems.append(f"放行批号 {d.lot_id} ≠ {want['lot']}")
            if d.scope != ReleaseScope.FULL:
                problems.append(f"{want['lot']} 应为整批放行")
            expect(f"{want['lot']} 放行量", d.quantity_kg, want["kg"])
            if list(d.report_ids) != [want["report"]] or list(d.sample_ids) != [want["sample"]]:
                problems.append(f"{want['lot']} 放行引用的报告/样品与预期不符")

    # 关键规则的内建断言
    if "dev-gap" not in pf.deviations:
        problems.append("温度缺口偏差缺失")
    gap = pf.deviations["dev-gap"]
    if gap.status != DeviationStatus.CLOSED:
        problems.append("温度缺口偏差未关闭")
    fry_a1 = pf.records["rec-fry-a1"]
    if fry_a1.backfill is None or not fry_a1.is_confirmed:
        problems.append("a-1 炒制记录补传/签署状态异常")
    fry_r1 = pf.records["rec-fry-r1"]
    if fry_r1.deviation_id is not None:
        problems.append("返工批新炒制记录不应直接挂原偏差")
    if pf.ledger.lots["a-2-r1"].rework_of != "a-2":
        problems.append("返工批未关联原批")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="炮制批次与质量放行核对视图")
    parser.add_argument("--check", action="store_true", help="机器核对 fixture 预期数据")
    parser.add_argument("--fixture", default=None, help="fixture JSON 路径")
    args = parser.parse_args(argv)

    data, result = load_fixture(args.fixture)
    print(render(data, result))

    if args.check:
        problems = check(data, result)
        print("\n" + "=" * 72)
        if problems:
            print("自动核对发现问题：")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("自动核对通过：物料守恒、缺口处理、报告引用与放行范围全部符合预期。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
