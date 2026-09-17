"""质量受权人（QP）放行评审与决定。

放行不是只看最终检验值，而是逐页核对连续批记录：

1. 物料台账全局守恒，批内每一克都有去向；
2. 净制→包装八道工序齐备，生效记录均已签署且参数/曲线合规；
3. 曲线缺口等异常均有偏差单，且已评估、验证、关闭；
4. 检验报告属于本批，所列样品确实取自本批且留样量可追溯；
5. 返工新批关联原批，并使用补采样品出具的新报告，禁止借用旧报告；
6. 放行范围（品种、批号、数量）与台账现存数量一致。

任何一条不满足，``decide`` 返回带原因的不予放行决定。
"""

from datetime import datetime
from decimal import Decimal

from .contracts import (
    LotStatus,
    RecordStatus,
    ReleaseDecision,
    Role,
    SampleKind,
    User,
)
from .deviations import DeviationManager
from .errors import ReleaseError
from .ledger import GRAM, MaterialLedger
from .quality import QualityLab
from .records import RecordBook
from .specifications import STANDARD_FLOW, SpecificationRegistry


class ReleaseReviewer:
    def __init__(
        self, ledger: MaterialLedger, records: RecordBook, lab: QualityLab,
        deviations: DeviationManager, specs: SpecificationRegistry,
    ) -> None:
        self.ledger = ledger
        self.records = records
        self.lab = lab
        self.deviations = deviations
        self.specs = specs
        self.decisions: dict[str, ReleaseDecision] = {}
        self._seq = 0

    def _decision_id(self) -> str:
        self._seq += 1
        return f"rel-{self._seq:03d}"

    # ------------------------------------------------------------- 评审资料
    def review_packet(self, lot_id: str) -> dict[str, object]:
        """组装质量受权人打开批次时看到的核对资料。"""
        lot = self.ledger.lot(lot_id)
        chain = self.lot_chain(lot_id)
        movements = [
            m for chain_lot in chain for m in self.ledger.movements_of(chain_lot)
        ]
        # 补入喂入链的外部批（如黄酒）的领料记录，保证来源完整
        feeder_sources = {
            s for m in movements for s in m.source_lots if s not in chain
        }
        movements.extend(
            m for s in feeder_sources for m in self.ledger.movements_of(s)
        )
        # 去重（返工移动在原批与新批各被收集一次）
        movements = list({m.movement_id: m for m in movements}.values())
        active = self.effective_records(lot_id)
        chain_records = [
            (chain_lot, r)
            for chain_lot in chain
            for r in self.records.for_lot(chain_lot)
        ]
        samples = [
            s for chain_lot in chain for s in self.lab.samples_of(chain_lot)
        ]
        deviations = [
            d for chain_lot in chain for d in self.deviations.for_lot(chain_lot)
        ]
        reports = [r for r in self.lab.reports.values() if r.lot_id in chain]

        gap_handling = []
        for chain_lot, rec in chain_records:
            if rec.status is RecordStatus.CORRECTED:
                gap_handling.append({
                    "record_id": rec.record_id, "lot_id": chain_lot,
                    "kind": rec.kind.value, "status": rec.status.value,
                    "note": "已签署原始记录冻结留痕，被偏差修正记录替代",
                })
            elif rec.deviation_id:
                gap_handling.append({
                    "record_id": rec.record_id, "lot_id": chain_lot,
                    "kind": rec.kind.value, "supersedes": rec.supersedes,
                    "deviation_id": rec.deviation_id,
                    "note": "凭偏差单出具的修正记录",
                })
        for chain_lot in chain:
            for rec in self.records.active_for_lot(chain_lot):
                problems = self.specs.validate_record(rec)
                if problems:
                    gap_handling.append({
                        "record_id": rec.record_id, "lot_id": chain_lot,
                        "kind": rec.kind.value, "open_problems": problems,
                    })
        for dev in deviations:
            gap_handling.append({
                "deviation_id": dev.deviation_id, "lot_id": dev.lot_id,
                "kind": dev.kind, "status": dev.status.value,
                "disposition": dev.disposition.value if dev.disposition else None,
                "impacted_records": list(dev.impacted_records),
            })

        return {
            "lot_chain": chain,
            "lot": self.ledger.lineage(lot_id),
            "material_trail": [
                {
                    "movement_id": m.movement_id,
                    "kind": m.kind.value,
                    "sources": list(m.source_lots),
                    "targets": list(m.target_lots),
                    "input_kg": str(m.input_kg),
                    "output_kg": str(m.output_kg),
                    "loss_kg": str(m.loss_kg),
                    "sample_kg": str(m.sample_kg),
                    "sample_id": m.sample_id,
                    "deviation_id": m.deviation_id,
                    "note": m.note,
                }
                for m in sorted(movements, key=lambda m: m.occurred_at)
            ],
            "reconciliation": {
                k: str(v) for k, v in self.ledger.reconciliation().items()
            },
            "process_records": [
                {
                    "record_id": r.record_id,
                    "owner_lot": owner,
                    "step": r.kind.value,
                    "version": r.specification_version,
                    "status": r.status.value,
                    "confirmed_by": r.confirmed_by,
                    "supersedes": r.supersedes,
                    "deviation_id": r.deviation_id,
                    "curve_points": len(r.curve),
                    "problems": self.specs.validate_record(r),
                }
                for owner, r in sorted(
                    ((o, r) for o, r in chain_records
                     if r.status is not RecordStatus.CORRECTED),
                    key=lambda x: (x[1].kind.value, x[0]),
                )
                if r.record_id == active.get(r.kind).record_id
            ],
            "superseded_records": [
                {
                    "record_id": r.record_id, "owner_lot": owner,
                    "step": r.kind.value, "deviation_id": r.deviation_id,
                }
                for owner, r in chain_records
                if r.status is RecordStatus.CORRECTED
            ],
            "deviations": [
                {
                    "deviation_id": d.deviation_id, "lot_id": d.lot_id,
                    "kind": d.kind, "status": d.status.value,
                    "disposition": d.disposition.value if d.disposition else None,
                    "title": d.title,
                }
                for d in deviations
            ],
            "gap_handling": gap_handling,
            "samples": [
                {
                    "sample_id": s.sample_id, "lot_id": s.lot_id,
                    "kind": s.kind.value,
                    "quantity_kg": str(s.quantity_kg),
                    "ledger_sample_kg": str(self.ledger.sample_balance(s.sample_id)),
                    "taken_by": s.taken_by,
                }
                for s in samples
            ],
            "reports": [
                {
                    "report_id": r.report_id,
                    "lot_id": r.lot_id,
                    "sample_ids": list(r.sample_ids),
                    "version": r.specification_version,
                    "conforms": r.conforms,
                    "external": r.external,
                    "results": [
                        {
                            "test": t.test_name, "value": str(t.value),
                            "limits": f"{t.lower}~{t.upper} {t.unit}",
                            "conforms": t.conforms,
                        }
                        for t in r.results
                    ],
                }
                for r in reports
            ],
            "release_scope": {
                "product": lot.product,
                "lot_id": lot_id,
                "quantity_kg": str(self.ledger.balance(lot_id)),
                "status": lot.status.value,
                "rework_of": lot.rework_of,
            },
        }

    # ------------------------------------------------------------- 批次链
    def lot_chain(self, lot_id: str) -> list[str]:
        """沿拆分（parents）与返工（rework_of）追溯的全部批号，本批在前。"""
        chain: list[str] = []
        stack = [lot_id]
        while stack:
            current = stack.pop()
            if current in chain:
                continue
            chain.append(current)
            info = self.ledger.lineage(current)
            stack.extend(info["parents"])
            if info["rework_of"]:
                stack.append(info["rework_of"])
        return chain

    def effective_records(self, lot_id: str) -> dict:
        """沿批次链汇总的现行生效记录：本批记录优先于祖先批。

        返工新批重做主工序（炒制/干燥/取样/包装），净制至润制沿原批继承；
        拆分子批继承拆分前的工序记录。
        """
        effective: dict = {}
        for ancestor in reversed(self.lot_chain(lot_id)):
            for rec in self.records.active_for_lot(ancestor):
                effective[rec.kind] = rec
        return effective

    # ------------------------------------------------------------- 放行校验
    def evaluate(self, lot_id: str, report_id: str | None = None) -> list[str]:
        """返回全部不予放行原因；空列表表示可以放行。"""
        reasons: list[str] = []
        lot = self.ledger.lot(lot_id)
        chain = self.lot_chain(lot_id)

        recon = self.ledger.reconciliation()
        if recon["on_hand_kg"] + recon["loss_kg"] + recon["sampled_kg"] != recon["received_kg"]:
            reasons.append("物料台账不守恒，无法证明每一克去向")

        active = self.effective_records(lot_id)
        for step in STANDARD_FLOW:
            rec = active.get(step)
            if rec is None:
                reasons.append(f"缺少工序记录：{step.value}")
                continue
            if rec.status is not RecordStatus.CONFIRMED:
                reasons.append(f"{step.value} 记录 {rec.record_id} 尚未签署")
            for problem in self.specs.validate_record(rec):
                reasons.append(f"{step.value}：{problem}")

        for chain_lot in chain:
            for dev in self.deviations.for_lot(chain_lot):
                if dev.status.value != "closed":
                    reasons.append(
                        f"批次链 {chain_lot} 的偏差 {dev.deviation_id}"
                        f"（{dev.kind}）状态为 {dev.status.value}，未关闭"
                    )

        referenced = self.lab.reports.get(report_id) if report_id else None
        if report_id is not None and referenced is None:
            reasons.append(
                f"报告 {report_id} 不在本企业检验台账中，"
                "不得引用上一批/外批报告放行"
            )
        if referenced is not None and referenced.lot_id != lot_id:
            reasons.append(
                f"报告 {report_id} 属于批号 {referenced.lot_id}，"
                f"不得借给 {lot_id} 放行（禁止借用旧报告）"
            )
        if referenced is not None and referenced.external:
            reasons.append(f"报告 {report_id} 为外批/历史报告，不能用于本批放行")

        report = self.lab.report_for(lot_id)
        if report is None:
            reasons.append("本批无检验报告")
        else:
            if report.lot_id != lot_id:
                reasons.append("检验报告所属批号与放行批号不一致")
            if not report.conforms:
                reasons.append(f"报告 {report.report_id} 存在不合格项")
            eff = self.specs.effective_version(lot.product, report.issued_at)
            if report.specification_version != eff.version:
                reasons.append(
                    f"报告按 {report.specification_version} 出具，"
                    f"现行版本为 {eff.version}"
                )
            for sid in report.sample_ids:
                sample = self.lab.samples.get(sid)
                if sample is None:
                    reasons.append(f"报告引用的样品 {sid} 不存在")
                    continue
                if sample.lot_id != lot_id:
                    reasons.append(
                        f"报告引用样品 {sid} 取自 {sample.lot_id}，"
                        "属于跨批借用"
                    )
                if self.ledger.sample_balance(sid) <= 0:
                    reasons.append(f"样品 {sid} 无留样台账记录")

        if lot.rework_of is not None:
            kinds = {s.kind for s in self.lab.samples_of(lot_id)}
            if not (kinds & {SampleKind.SUPPLEMENT, SampleKind.RETEST}):
                reasons.append("返工新批必须补采（supplement/retest）并重新检验")
            parent = self.ledger.lot(lot.rework_of)
            if parent.status is not LotStatus.QUARANTINED:
                reasons.append(
                    f"返工原批 {lot.rework_of} 未隔离，仍可能被重复放行"
                )

        if lot.status is LotStatus.REJECTED:
            reasons.append("批号已判定拒绝放行")
        if lot.status is LotStatus.QUARANTINED and lot.rework_of is None:
            reasons.append(f"批号 {lot_id} 处于隔离状态，不得放行")
        return reasons

    def decide(
        self, actor: User, lot_id: str, at: datetime,
        *, report_id: str | None = None,
        scope_quantity_kg: Decimal | None = None,
    ) -> ReleaseDecision:
        if actor.role is not Role.QP:
            raise ReleaseError(f"角色 {actor.role.value} 无权放行，须由质量受权人决定")
        lot = self.ledger.lot(lot_id)
        on_hand = self.ledger.balance(lot_id)
        qty = on_hand if scope_quantity_kg is None else Decimal(scope_quantity_kg).quantize(GRAM)
        reasons = self.evaluate(lot_id, report_id)
        if qty != on_hand:
            reasons.append(
                f"放行数量 {qty} kg 与台账现存 {on_hand} kg 不一致，"
                "放行范围必须与批记录一致"
            )

        chosen = self.lab.report_for(lot_id)
        if report_id is not None and self.lab.reports.get(report_id) is not chosen:
            chosen = self.lab.reports.get(report_id)
        if chosen and chosen.lot_id != lot_id:
            chosen = None
        sample_ids = tuple(chosen.sample_ids) if chosen else ()
        deviation_ids = tuple(
            d.deviation_id for chain_lot in self.lot_chain(lot_id)
            for d in self.deviations.for_lot(chain_lot)
        )
        approved = not reasons
        decision = ReleaseDecision(
            decision_id=self._decision_id(),
            lot_id=lot_id, sample_ids=sample_ids,
            deviation_ids=deviation_ids,
            scope=f"{lot.product} / {lot_id} / {qty} kg",
            decided_by=actor.user_id, decided_at=at,
            product=lot.product,
            report_id=chosen.report_id if chosen else report_id,
            approved=approved, quantity_kg=qty,
            rejected_reasons=tuple(reasons),
        )
        self.decisions[decision.decision_id] = decision
        if approved:
            self.ledger.set_status(lot_id, LotStatus.RELEASED)
        return decision
