"""质量受权人样例：python -m process.demo

打开酒炙当归 proc-0915 批，按「物料去向 → 缺口处理 → 报告与样品 →
放行范围」四步打印连续批记录核对结果。
"""

import json

from .scenario import build_scenario


def _line(char: str = "=", n: int = 72) -> str:
    return char * n


def show_packet(title: str, packet: dict) -> None:
    print(_line())
    print(title)
    print(_line("-"))
    print("【批次链】", " → ".join(packet["lot_chain"]))
    scope = packet["release_scope"]
    print(f"【放行范围】{scope['product']} / {scope['lot_id']} / "
          f"{scope['quantity_kg']} kg（状态 {scope['status']}，"
          f"返工自 {scope['rework_of'] or '—'}）")

    recon = packet["reconciliation"]
    print(f"【全局守恒】领料 {recon['received_kg']} = 现存 {recon['on_hand_kg']}"
          f" + 损耗 {recon['loss_kg']} + 留样 {recon['sampled_kg']} kg")

    print("【每一克去向】")
    for m in packet["material_trail"]:
        extra = []
        if m["loss_kg"] != "0.000":
            extra.append(f"损耗 {m['loss_kg']}")
        if m["sample_kg"] != "0.000":
            extra.append(f"留样 {m['sample_kg']}（{m['sample_id']}）")
        if m["deviation_id"]:
            extra.append(f"挂偏差 {m['deviation_id']}")
        print(f"  {m['movement_id']} {m['kind']:<8} "
              f"{','.join(m['sources']) or '领料':<18}→ {','.join(m['targets']) or '—':<20}"
              f"投 {m['input_kg']:>7} / 产 {m['output_kg']:>7}"
              f"{'  ' + '，'.join(extra) if extra else ''}")

    print("【缺口与修正处理】")
    for g in packet["gap_handling"]:
        if "deviation_id" in g and "open_problems" not in g and "status" in g:
            print(f"  偏差 {g['deviation_id']}（{g.get('lot_id')}，{g['kind']}）"
                  f"状态 {g['status']}，处置 {g['disposition']}")
        elif "open_problems" in g:
            print(f"  ! {g['record_id']}（{g['kind']}）未决问题："
                  f"{'；'.join(g['open_problems'])}")
        else:
            print(f"  记录 {g['record_id']}（{g.get('lot_id')}，{g['kind']}）"
                  f"{g.get('status', '修正')}：{g['note']}"
                  f"{g.get('deviation_id', '') and '（' + g['deviation_id'] + '）'}")

    print("【检验报告 ↔ 样品】")
    for s in packet["samples"]:
        print(f"  样品 {s['sample_id']} 取自 {s['lot_id']}（{s['kind']}），"
              f"台账留样 {s['ledger_sample_kg']} kg")
    for r in packet["reports"]:
        tag = "（外部/历史报告，禁止借用）" if r["external"] else ""
        print(f"  报告 {r['report_id']} → 批 {r['lot_id']}，"
              f"样品 {','.join(r['sample_ids'])}，"
              f"全项合格={r['conforms']}{tag}")
    print()


def main() -> None:
    scenario = build_scenario()
    pf = scenario.platform

    show_packet(
        "一、返工新批 proc-0915-a-2-r1 的放行核对资料",
        pf.reviewer.review_packet("proc-0915-a-2-r1"),
    )

    print(_line())
    print("二、四个放行决定")
    print(_line("-"))
    labels = {
        "borrow": "车间引用上一批 rpt-0914-legacy 为 a-2 申请放行",
        "release_a1": "a-1（签署前补传缺口）申请放行",
        "release_r1": "返工新批 a-2-r1（补采+新报告）申请放行",
        "reject_a2": "原批 a-2 返工后再次申请放行",
    }
    for key, decision in scenario.decisions.items():
        verdict = "准予放行" if decision.approved else "不予放行"
        print(f"■ {labels[key]}")
        print(f"  决定 {decision.decision_id}：{verdict}")
        print(f"  范围：{decision.scope}")
        print(f"  引用报告：{decision.report_id or '—'}；"
              f"样品：{','.join(decision.sample_ids) or '—'}")
        if not decision.approved:
            for reason in decision.rejected_reasons:
                print(f"    - {reason}")
        print()

    print(_line())
    print("三、机器可读核对包（JSON 摘要）")
    print(_line("-"))
    packet = pf.reviewer.review_packet("proc-0915-a-2-r1")
    print(json.dumps(
        {
            "lot_chain": packet["lot_chain"],
            "reconciliation": packet["reconciliation"],
            "superseded_records": packet["superseded_records"],
            "release_scope": packet["release_scope"],
        },
        ensure_ascii=False, indent=2,
    ))


if __name__ == "__main__":
    main()
