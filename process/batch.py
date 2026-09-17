"""炮制批次与质量放行平台。

把物料台账（:mod:`process.ledger`）、版本化工艺规格（:mod:`process.specs`）
与工序记录、偏差、返工、取样复检、放行决定串成一条受权限控制的连续批记录。

职责分离
========
* 操作人 ``OPERATOR``：领料、拆分合批、执行并 *签署* 工序记录、传感器补传
  （补传仅限未签署记录）；
* 质量 ``QA``：取样、出具报告、发起/执行补采复检，可登记偏差；
* 偏差管理员 ``DEVIATION_OWNER``：评估偏差、凭已评估偏差单修正已签署曲线、关闭偏差；
* 工艺员 ``REWORK_PLANNER``：批准返工、建返工批、确认返工完成；
* 质量受权人 ``QP``：唯一的放行决定人。
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from .contracts import (
    ASSESSED_DISPOSITIONS,
    CurvePoint,
    Deviation,
    DeviationKind,
    DeviationStatus,
    MaterialMovement,
    MovementKind,
    ProcessKind,
    ProcessRecord,
    PROCESS_ORDER,
    ReleaseDecision,
    ReleaseScope,
    ReworkOrder,
    Role,
    Sample,
    SampleStatus,
    SensorBackfill,
    TestReport,
)
from .errors import (
    AuthorizationError,
    ConservationError,
    DeviationStateError,
    PlatformError,
    RecordStateError,
    ReleaseBlocked,
    ReportBorrowingError,
    SpecificationError,
)
from .ledger import ZERO, Ledger, _q
from .specs import ProcessSpecification, SpecificationRegistry
from .users import User

# 返工批必须重新执行的关键工序（前段净/切/加酒/润经谱系继承，炒制起重做）。
REWORK_STEPS: tuple[ProcessKind, ...] = (
    ProcessKind.FRY,
    ProcessKind.DRY,
    ProcessKind.SAMPLE,
    ProcessKind.PACK,
)


@dataclass(frozen=True)
class AuditEntry:
    at: datetime
    actor: str
    action: str
    target: str
    detail: str = ""
    allowed: bool = True


class ProcessingPlatform:
    def __init__(self, registry: SpecificationRegistry) -> None:
        self.registry = registry
        self.ledger = Ledger()
        self.records: dict[str, ProcessRecord] = {}
        self.deviations: dict[str, Deviation] = {}
        self.reworks: dict[str, ReworkOrder] = {}
        self.samples: dict[str, Sample] = {}
        self.reports: dict[str, TestReport] = {}
        self.releases: list[ReleaseDecision] = []
        self.audit: list[AuditEntry] = []
        self._seq = 0

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _log(self, at: datetime, user: User, action: str, target: str,
             detail: str = "", *, allowed: bool = True) -> None:
        self.audit.append(
            AuditEntry(at, user.user_id, action, target, detail, allowed)
        )

    def _mid(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:03d}"

    def _gaps(self, spec: ProcessSpecification, record: ProcessRecord):
        """返回炒制曲线缺口列表 (起点秒, 终点秒, 间隔秒)。"""
        if record.kind != ProcessKind.FRY or spec.curve_rule is None or not record.curve:
            return []
        rule = spec.curve_rule
        pts = sorted(record.curve, key=lambda p: p.t_seconds)
        return [
            (prev.t_seconds, nxt.t_seconds, nxt.t_seconds - prev.t_seconds)
            for prev, nxt in zip(pts, pts[1:])
            if rule.check_interval(nxt.t_seconds - prev.t_seconds) is not None
        ]

    def _record_deviations(self, record_id: str) -> list[Deviation]:
        """覆盖某条记录的偏差：记录自身挂接 或 偏差单关联该记录。"""
        result = []
        for dev in self.deviations.values():
            rec = self.records.get(record_id)
            if dev.linked_record_id == record_id or (
                rec is not None and rec.deviation_id == dev.deviation_id
            ):
                result.append(dev)
        return result

    # ------------------------------------------------------------------
    # 领料建账 / 拆分 / 合批
    # ------------------------------------------------------------------
    def issue(self, user: User, lot_id: str, product: str, kg: Decimal,
              at: datetime, *, is_excipient: bool = False) -> None:
        user.require(Role.OPERATOR)
        kg = Decimal(kg)
        self.ledger.open_lot(
            lot_id, product, kg, created_at=at, is_excipient=is_excipient
        )
        self.ledger.post(
            MaterialMovement(
                movement_id=self._mid("mv-issue"),
                kind=MovementKind.ISSUE,
                source_lots=(),
                target_lots=(lot_id,),
                input_kg=kg,
                output_kg=kg,
                loss_kg=ZERO,
                occurred_at=at,
            )
        )
        self._log(at, user, "issue", lot_id, f"领料 {kg} kg")

    def split_lot(self, user: User, source: str, outputs: dict[str, Decimal],
                  loss_kg: Decimal, at: datetime) -> str:
        """整笔拆批：``来源扣减 = 各产出之和 + 损耗``，逐克守恒。"""
        user.require(Role.OPERATOR)
        product = self.ledger.lots[source].product
        for lot_id, amount in outputs.items():
            if Decimal(amount) < 0:
                raise ConservationError("拆批产出不得为负")
            self.ledger.open_lot(lot_id, product, ZERO, created_at=at)
        out_total = sum((Decimal(v) for v in outputs.values()), ZERO)
        loss_kg = Decimal(loss_kg)
        mid = self._mid("mv-split")
        self.ledger.post(
            MaterialMovement(
                movement_id=mid,
                kind=MovementKind.SPLIT,
                source_lots=(source,),
                target_lots=tuple(outputs),
                input_kg=_q(out_total + loss_kg),
                output_kg=_q(out_total),
                loss_kg=_q(loss_kg),
                occurred_at=at,
                target_amounts=tuple(_q(v) for v in outputs.values()),
            )
        )
        self._log(at, user, "split", source,
                  f"拆出 {dict(outputs)}，损耗 {loss_kg} kg，单号 {mid}")
        return mid

    # ------------------------------------------------------------------
    # 工序记录
    # ------------------------------------------------------------------
    def record_process(
        self,
        user: User,
        record_id: str,
        lot_id: str,
        kind: ProcessKind,
        version: str,
        started_at: datetime,
        ended_at: datetime | None,
        parameters: dict[str, str],
        *,
        input_kg: Decimal | None = None,
        output_kg: Decimal | None = None,
        curve: tuple[CurvePoint, ...] = (),
        sensor_digest: str | None = None,
        at: datetime,
    ) -> ProcessRecord:
        user.require(Role.OPERATOR)
        product = self.ledger.lots[lot_id].product
        spec = self.registry.require_effective(product, version, started_at)
        spec.validate_parameters(kind, parameters)
        if curve:
            spec.validate_curve(curve)  # 仅校验温度窗口；缺口走偏差流程
        self._check_step_order(lot_id, kind, started_at)

        record = ProcessRecord(
            record_id=record_id,
            lot_id=lot_id,
            kind=kind,
            product=product,
            specification_version=version,
            order_no=PROCESS_ORDER[kind],
            started_at=started_at,
            ended_at=ended_at,
            parameters=dict(parameters),
            curve=tuple(curve),
            sensor_digest=sensor_digest,
        )
        self.records[record_id] = record

        if input_kg is not None or output_kg is not None:
            if input_kg is None or output_kg is None:
                raise PlatformError(f"工序 {record_id} 的投入/产出量必须成对给出")
            self._post_weight_change(
                record_id, lot_id, Decimal(input_kg), Decimal(output_kg),
                ended_at or started_at,
            )
        self._log(at, user, "record", record_id,
                  f"{kind.value}@{version} 记录建立（未签署）")
        return record

    def _post_weight_change(self, ref: str, lot_id: str,
                            input_kg: Decimal, output_kg: Decimal,
                            at: datetime) -> None:
        """同批加工：批不转移，``投入-产出`` 之差以有符号损耗改变结存。"""
        self.ledger.post(
            MaterialMovement(
                movement_id=self._mid("mv-consume"),
                kind=MovementKind.CONSUME,
                source_lots=(lot_id,),
                target_lots=(lot_id,),
                input_kg=input_kg,
                output_kg=output_kg,
                loss_kg=_q(input_kg - output_kg),
                occurred_at=at,
                ref=ref,
            )
        )

    def add_excipient(self, user: User, record_id: str, lot_id: str,
                      excipient_lot: str, wine_kg: Decimal, version: str,
                      started_at: datetime, ended_at: datetime,
                      parameters: dict[str, str]) -> ProcessRecord:
        """辅料加入：黄酒从辅料批守恒地转入药材批（药材批增重可逐克追溯）。"""
        user.require(Role.OPERATOR)
        product = self.ledger.lots[lot_id].product
        spec = self.registry.require_effective(product, version, started_at)
        spec.validate_parameters(ProcessKind.ADD_EXCIPIENT, parameters)
        self._check_step_order(lot_id, ProcessKind.ADD_EXCIPIENT, started_at)
        wine_kg = Decimal(wine_kg)

        record = ProcessRecord(
            record_id=record_id,
            lot_id=lot_id,
            kind=ProcessKind.ADD_EXCIPIENT,
            product=product,
            specification_version=version,
            order_no=PROCESS_ORDER[ProcessKind.ADD_EXCIPIENT],
            started_at=started_at,
            ended_at=ended_at,
            parameters=dict(parameters),
        )
        self.records[record_id] = record
        self.ledger.post(
            MaterialMovement(
                movement_id=self._mid("mv-add-excipient"),
                kind=MovementKind.MERGE,
                source_lots=(excipient_lot, lot_id),
                target_lots=(lot_id,),
                input_kg=wine_kg,
                output_kg=wine_kg,
                loss_kg=ZERO,
                occurred_at=ended_at,
                source_amounts=(wine_kg, ZERO),
                target_amounts=(wine_kg,),
                ref=record_id,
            )
        )
        self._log(ended_at, user, "add_excipient", record_id,
                  f"辅料批 {excipient_lot} 转入 {wine_kg} kg")
        return record

    # 实质工序序（取样 70 不阻塞后续工序，放行闸门再强制“取样必须存在”）。
    MATERIAL_ORDERS: tuple[int, ...] = (10, 20, 30, 40, 50, 60, 80)

    def _material_cursor(self, lot_id: str) -> int:
        """沿物料谱系已完成的实质工序号；返工批从炒制（50）前重新推进。"""
        lot = self.ledger.lots[lot_id]
        if lot.rework_of is not None:
            floor = PROCESS_ORDER[ProcessKind.MOISTEN]  # 前段经原批谱系继承
        else:
            floor = 0
            ancestors = set(self.upstream_lots(lot_id)) - {lot_id}
            floor = max(
                (r.order_no for r in self.records.values()
                 if r.lot_id in ancestors and r.kind != ProcessKind.SAMPLE),
                default=0,
            )
        local = max(
            (r.order_no for r in self.records.values()
             if r.lot_id == lot_id and r.kind != ProcessKind.SAMPLE),
            default=0,
        )
        return max(floor, local)

    def _check_step_order(self, lot_id: str, kind: ProcessKind,
                          started_at: datetime) -> None:
        """连续批记录：实质工序沿谱系严格按序、不得跳工序或倒序。

        拆分子批可从共同前处理的下一步开始；返工批自炒制起重做。
        取样（含补采）只允许出现在干燥之后，可在包装前后穿插、可重复。
        """
        if kind == ProcessKind.SAMPLE:
            if self._material_cursor(lot_id) < PROCESS_ORDER[ProcessKind.DRY]:
                raise SpecificationError(
                    f"批号 {lot_id} 取样只能发生在干燥工序之后"
                )
            return

        new_order = PROCESS_ORDER[kind]
        cursor = self._material_cursor(lot_id)
        seq = self.MATERIAL_ORDERS
        if cursor == 0:
            expected = seq[0]
        elif cursor in seq:
            idx = seq.index(cursor)
            expected = seq[idx + 1] if idx + 1 < len(seq) else None
        else:  # pragma: no cover - cursor 恒为实质工序号
            expected = None
        if expected is None or new_order < cursor:
            raise SpecificationError(
                f"批号 {lot_id} 工序 {kind.value}（{new_order}）倒序或越界；"
                f"当前实质进度 {cursor}"
            )
        if new_order != expected:
            raise SpecificationError(
                f"批号 {lot_id} 工序断裂：{kind.value}（{new_order}）之前应先完成"
                f"工序号 {expected}（净制→切制→加酒→润→炒→干燥→[取样]→包装）"
            )

    # ------------------------------------------------------------------
    # 传感器补传 / 签署 / 偏差修正
    # ------------------------------------------------------------------
    def backfill_sensor(self, user: User, record_id: str,
                        points: tuple[CurvePoint, ...], payload_digest: str,
                        received_at: datetime,
                        gap: tuple[int, int] | None = None) -> ProcessRecord:
        """传感器补传：只能写入 *未签署* 记录。"""
        user.require(Role.OPERATOR)
        record = self.records[record_id]
        if record.is_confirmed:
            raise RecordStateError(
                f"记录 {record_id} 已由 {record.confirmed_by} 签署，传感器补传不得"
                f"覆盖已确认曲线；须开具偏差单后由偏差管理员修正"
            )
        spec = self.registry.get(record.product, record.specification_version)
        merged = tuple(sorted((*record.curve, *points), key=lambda p: p.t_seconds))
        spec.validate_curve(merged)
        record = replace(
            record,
            curve=merged,
            sensor_digest=payload_digest,
            backfill=SensorBackfill(payload_digest, received_at, gap),
        )
        self.records[record_id] = record
        gap_note = f"，填补 {gap[0]}~{gap[1]}s 缺口" if gap else ""
        self._log(received_at, user, "backfill", record_id,
                  f"补传 {len(points)} 个采样点{gap_note}")
        return record

    def confirm_record(self, user: User, record_id: str, at: datetime) -> ProcessRecord:
        user.require(Role.OPERATOR)
        record = self.records[record_id]
        if record.is_confirmed:
            raise RecordStateError(f"记录 {record_id} 已签署，不得重复签署")
        if record.kind == ProcessKind.FRY and not record.curve:
            raise RecordStateError(f"炒制记录 {record_id} 无温度曲线，不得签署")
        record = replace(record, confirmed_by=user.user_id, confirmed_at=at)
        self.records[record_id] = record
        self._log(at, user, "confirm", record_id, "操作人签署并冻结记录")
        return record

    def correct_confirmed_curve(self, user: User, record_id: str,
                                deviation_id: str,
                                corrected_curve: tuple[CurvePoint, ...],
                                at: datetime) -> ProcessRecord:
        """已签署曲线只能凭 *已评估* 偏差单由偏差管理员修正（原值留存审计）。"""
        user.require(Role.DEVIATION_OWNER)
        record = self.records.get(record_id)
        if record is None or not record.is_confirmed:
            raise RecordStateError(
                f"记录 {record_id} 不存在或尚未签署；未签署记录应走传感器补传"
            )
        dev = self.deviations.get(deviation_id)
        if dev is None:
            raise DeviationStateError(f"偏差单 {deviation_id} 不存在")
        if dev.status not in ASSESSED_DISPOSITIONS:
            raise DeviationStateError(
                f"偏差单 {deviation_id} 尚未完成评估，不得据以修正曲线"
            )
        if dev.linked_record_id not in (None, record_id):
            raise DeviationStateError(
                f"偏差单 {deviation_id} 关联的是记录 {dev.linked_record_id}"
            )
        spec = self.registry.get(record.product, record.specification_version)
        spec.validate_curve(corrected_curve)
        record = replace(
            record, curve=tuple(corrected_curve), deviation_id=deviation_id
        )
        self.records[record_id] = record
        if dev.linked_record_id is None:
            self.deviations[deviation_id] = replace(dev, linked_record_id=record_id)
        self._log(at, user, "curve_correction", record_id,
                  f"依据偏差单 {deviation_id} 修正已签署曲线")
        return record

    # ------------------------------------------------------------------
    # 偏差工作流
    # ------------------------------------------------------------------
    def open_deviation(self, user: User, deviation_id: str, lot_id: str,
                       kind: DeviationKind, title: str, detail: str,
                       at: datetime, *,
                       linked_record_id: str | None = None) -> Deviation:
        if not (user.has(Role.DEVIATION_OWNER) or user.has(Role.QA)):
            raise AuthorizationError("只有偏差管理员/QA 可以登记偏差")
        dev = Deviation(
            deviation_id=deviation_id,
            lot_id=lot_id,
            kind=kind,
            title=title,
            detail=detail,
            opened_by=user.user_id,
            opened_at=at,
            status=DeviationStatus.OPEN,
            linked_record_id=linked_record_id,
        )
        self.deviations[deviation_id] = dev
        self._log(at, user, "deviation_open", deviation_id, title)
        return dev

    def assess_deviation(self, user: User, deviation_id: str,
                         assessment: str, disposition: DeviationStatus,
                         at: datetime) -> Deviation:
        user.require(Role.DEVIATION_OWNER)
        dev = self.deviations[deviation_id]
        if dev.status != DeviationStatus.OPEN:
            raise DeviationStateError(
                f"偏差单 {deviation_id} 状态为 {dev.status.value}，不能重复评估"
            )
        if disposition not in ASSESSED_DISPOSITIONS:
            raise DeviationStateError(f"评估结论 {disposition} 非法")
        dev = replace(
            dev,
            status=disposition,
            assessed_by=user.user_id,
            assessed_at=at,
            assessment=assessment,
            disposition=disposition,
        )
        self.deviations[deviation_id] = dev
        # 评估结论生效后，在所关联工序记录上留痕（记录 ↔ 偏差双向可查）。
        if dev.linked_record_id and dev.linked_record_id in self.records:
            rec = self.records[dev.linked_record_id]
            if rec.deviation_id is None:
                self.records[dev.linked_record_id] = replace(
                    rec, deviation_id=deviation_id
                )
        self._log(at, user, "deviation_assess", deviation_id,
                  f"评估结论：{disposition.value}。{assessment}")
        return dev

    def close_deviation(self, user: User, deviation_id: str, note: str,
                        at: datetime) -> Deviation:
        user.require(Role.DEVIATION_OWNER)
        dev = self.deviations[deviation_id]
        if dev.disposition is None or dev.status not in ASSESSED_DISPOSITIONS:
            raise DeviationStateError(f"偏差单 {deviation_id} 尚未评估，不能关闭")

        if dev.disposition == DeviationStatus.REWORK_APPROVED:
            orders = [o for o in self.reworks.values() if o.deviation_id == deviation_id]
            if not orders or not all(o.completed for o in orders):
                raise DeviationStateError(
                    f"偏差单 {deviation_id} 的返工尚未完成，不能关闭"
                )
        elif dev.disposition == DeviationStatus.RETEST_APPROVED:
            report = self.reports.get(dev.linked_report_id) if dev.linked_report_id else None
            if report is None or report.invalidated or not report.is_conforming:
                raise DeviationStateError(
                    f"偏差单 {deviation_id} 的补采复检报告缺失或仍不合格，不能关闭"
                )
        elif dev.disposition == DeviationStatus.CONCESSION_APPROVED:
            if not note:
                raise DeviationStateError("让步关闭必须填写调查/纠正说明")
        elif dev.disposition == DeviationStatus.REJECTED:
            if self.ledger.balance(dev.lot_id).balance_kg > 0:
                raise DeviationStateError(
                    f"偏差单 {deviation_id} 判定报废，但批号 {dev.lot_id} 仍有结存"
                )

        dev = replace(
            dev, status=DeviationStatus.CLOSED, closed_by=user.user_id,
            closed_at=at, closure_note=note,
        )
        self.deviations[deviation_id] = dev
        self._log(at, user, "deviation_close", deviation_id, note)
        return dev

    # ------------------------------------------------------------------
    # 返工
    # ------------------------------------------------------------------
    def plan_rework(self, user: User, deviation_id: str, rework_lot: str,
                    at: datetime) -> ReworkOrder:
        user.require(Role.REWORK_PLANNER)
        dev = self.deviations[deviation_id]
        if dev.status != DeviationStatus.REWORK_APPROVED:
            raise DeviationStateError(
                f"偏差单 {deviation_id} 未批准返工（当前 {dev.status.value}）"
            )
        if rework_lot in self.ledger.lots:
            raise ConservationError(f"返工批号 {rework_lot} 已存在")
        amount = self.ledger.balance(dev.lot_id).balance_kg
        if amount <= 0:
            raise ConservationError(f"原批 {dev.lot_id} 无可用结存，无法返工")
        product = self.ledger.lots[dev.lot_id].product
        version = self.registry.effective_version(product, at)

        order = ReworkOrder(
            rework_id=self._mid("rw"),
            original_lot=dev.lot_id,
            rework_lot=rework_lot,
            deviation_id=deviation_id,
            product=product,
            specification_version=version,
            planned_by=user.user_id,
            planned_at=at,
        )
        self.reworks[order.rework_id] = order
        self.ledger.open_lot(rework_lot, product, ZERO, created_at=at,
                             rework_of=dev.lot_id)
        self.ledger.post(
            MaterialMovement(
                movement_id=self._mid("mv-rework"),
                kind=MovementKind.REWORK,
                source_lots=(dev.lot_id,),
                target_lots=(rework_lot,),
                input_kg=amount,
                output_kg=amount,
                loss_kg=ZERO,
                occurred_at=at,
                source_amounts=(amount,),
                target_amounts=(amount,),
                ref=deviation_id,
            )
        )
        self._log(at, user, "rework_plan", rework_lot,
                  f"依据偏差单 {deviation_id} 从原批 {dev.lot_id} 转入 {amount} kg；"
                  f"新批独立加工、独立取样、独立出报告")
        return order

    def complete_rework(self, user: User, rework_id: str, at: datetime) -> ReworkOrder:
        user.require(Role.REWORK_PLANNER)
        order = self.reworks[rework_id]
        if order.completed:
            raise DeviationStateError(f"返工单 {rework_id} 已完成")
        recs = [r for r in self.records.values() if r.lot_id == order.rework_lot]
        kinds = {r.kind for r in recs}
        missing = [k.value for k in REWORK_STEPS if k not in kinds]
        if missing:
            raise DeviationStateError(
                f"返工批 {order.rework_lot} 缺少关键工序 {missing}，不能完成返工"
            )
        unconfirmed = [r.record_id for r in recs if not r.is_confirmed]
        if unconfirmed:
            raise DeviationStateError(
                f"返工批 {order.rework_lot} 存在未签署记录 {unconfirmed}"
            )
        order = replace(order, completed=True, completed_at=at)
        self.reworks[rework_id] = order
        self._log(at, user, "rework_complete", order.rework_lot,
                  f"返工 {rework_id} 加工与签署完成")
        return order

    # ------------------------------------------------------------------
    # 取样、检验、复检
    # ------------------------------------------------------------------
    def draw_sample(self, user: User, sample_id: str, lot_id: str,
                    from_record: str, kg: Decimal, at: datetime) -> Sample:
        user.require(Role.QA)
        record = self.records.get(from_record)
        if record is None or record.lot_id != lot_id:
            raise PlatformError(f"取样单 {sample_id} 引用的工序记录不属于批号 {lot_id}")
        if not record.is_confirmed:
            raise RecordStateError(
                f"取样所依据的记录 {from_record} 尚未签署，样品不能代表已确认工艺状态"
            )
        kg = Decimal(kg)
        sample = Sample(
            sample_id=sample_id,
            lot_id=lot_id,
            taken_from_record=from_record,
            quantity_kg=kg,
            drawn_by=user.user_id,
            drawn_at=at,
            status=SampleStatus.DRAWN,
        )
        self.samples[sample_id] = sample
        # 取样本身是连续批记录的一环：生成一条已签署的 SAMPLE 工序记录，
        # 版本沿用所跟工序记录，保证谱系链条上“取样”可核对。
        source = self.records[from_record]
        self.records[f"rec-{sample_id}"] = ProcessRecord(
            record_id=f"rec-{sample_id}",
            lot_id=lot_id,
            kind=ProcessKind.SAMPLE,
            product=source.product,
            specification_version=source.specification_version,
            order_no=PROCESS_ORDER[ProcessKind.SAMPLE],
            started_at=at,
            ended_at=at,
            parameters={"sample_kg": str(kg), "taken_from_record": from_record},
            confirmed_by=user.user_id,
            confirmed_at=at,
        )
        self.ledger.post(
            MaterialMovement(
                movement_id=self._mid("mv-sample"),
                kind=MovementKind.SAMPLE,
                source_lots=(lot_id,),
                target_lots=(),
                input_kg=kg,
                output_kg=ZERO,
                loss_kg=kg,
                occurred_at=at,
                source_amounts=(kg,),
                ref=sample_id,
            )
        )
        self._log(at, user, "sample_draw", sample_id,
                  f"从 {lot_id}（记录 {from_record}）取样 {kg} kg")
        return sample

    def issue_report(self, user: User, report_id: str, sample_id: str,
                     results: dict[str, Decimal],
                     limits: dict[str, tuple[Decimal, Decimal]],
                     at: datetime, *, supersedes: str | None = None) -> TestReport:
        user.require(Role.QA)
        sample = self.samples[sample_id]
        if sample.status == SampleStatus.INVALIDATED:
            raise ReportBorrowingError(f"样品 {sample_id} 已作废，不得再出具报告")
        if set(results) - set(limits):
            raise PlatformError(f"报告 {report_id} 存在缺少限度的检验项")
        lot = self.ledger.lots[sample.lot_id]
        report = TestReport(
            report_id=report_id,
            sample_id=sample_id,
            lot_id=sample.lot_id,
            product=lot.product,
            specification_version=self._lot_version(sample.lot_id),
            issued_at=at,
            results={k: Decimal(v) for k, v in results.items()},
            limits={k: (Decimal(lo), Decimal(hi)) for k, (lo, hi) in limits.items()},
            supersedes=supersedes,
        )
        self.reports[report_id] = report
        self.samples[sample_id] = replace(
            sample, status=SampleStatus.TESTED, report_id=report_id
        )
        verdict = "合格" if report.is_conforming else "不合格"
        self._log(at, user, "report_issue", report_id,
                  f"样品 {sample_id} / 批号 {sample.lot_id}，结论：{verdict}")
        return report

    def retest(self, user: User, deviation_id: str, new_sample_id: str,
               from_record: str, sample_kg: Decimal, new_report_id: str,
               results: dict[str, Decimal],
               limits: dict[str, tuple[Decimal, Decimal]],
               at: datetime) -> TestReport:
        """偏差批准复检后的 *补采复检*：新样品、新报告；旧报告/旧样品作废。"""
        user.require(Role.QA)
        dev = self.deviations[deviation_id]
        if dev.status != DeviationStatus.RETEST_APPROVED:
            raise DeviationStateError(
                f"偏差单 {deviation_id} 未批准复检（当前 {dev.status.value}）"
            )
        sample = self.draw_sample(
            user, new_sample_id, dev.lot_id, from_record, sample_kg, at
        )
        prior = [
            r for r in self.reports.values()
            if r.lot_id == dev.lot_id and not r.invalidated
        ]
        report = self.issue_report(
            user, new_report_id, new_sample_id, results, limits, at,
            supersedes=prior[-1].report_id if prior else None,
        )
        for old in prior:
            self.reports[old.report_id] = replace(old, invalidated=True)
            old_sample = self.samples[old.sample_id]
            if old_sample.status != SampleStatus.INVALIDATED:
                self.samples[old.sample_id] = replace(
                    old_sample, status=SampleStatus.INVALIDATED
                )
        self.deviations[deviation_id] = replace(
            dev, linked_report_id=new_report_id,
            linked_record_id=dev.linked_record_id or from_record,
        )
        self._log(at, user, "retest", new_report_id,
                  f"依据偏差单 {deviation_id} 补采复检，作废旧报告 "
                  f"{[r.report_id for r in prior]}")
        return report

    def _lot_version(self, lot_id: str) -> str:
        versions = {
            r.specification_version
            for r in self.records.values() if r.lot_id == lot_id
        }
        return next(iter(versions)) if versions else "-"

    # ------------------------------------------------------------------
    # 谱系与连续批记录
    # ------------------------------------------------------------------
    def upstream_lots(self, lot_id: str) -> list[str]:
        """沿拆分/合批/返工移动向上追溯全部祖先批号（含自身，近→远）。"""
        seen: list[str] = []
        stack = [lot_id]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.append(current)
            for m in self.ledger.movements:
                if current in m.target_lots:
                    for src in m.source_lots:
                        if src not in seen:
                            stack.append(src)
        return seen

    def record_chain(self, lot_id: str) -> list[ProcessRecord]:
        """终端批号沿物料谱系继承的完整连续批记录（按工艺顺序）。"""
        lots = set(self.upstream_lots(lot_id))
        records = [r for r in self.records.values() if r.lot_id in lots]
        records.sort(key=lambda r: (r.order_no, r.started_at))
        return records

    # ------------------------------------------------------------------
    # 放行
    # ------------------------------------------------------------------
    def release(self, user: User, lot_id: str, report_ids: tuple[str, ...],
                at: datetime, *, scope: ReleaseScope = ReleaseScope.FULL,
                quantity_kg: Decimal | None = None,
                rationale: str = "") -> ReleaseDecision:
        user.require(Role.QP)
        failures = self._evaluate_release(lot_id, report_ids, scope, quantity_kg)
        if failures:
            self._log(at, user, "release_rejected", lot_id,
                      "；".join(failures), allowed=False)
            raise ReleaseBlocked(failures)

        chain_lots = self.upstream_lots(lot_id)
        sample_ids = tuple(self.reports[r].sample_id for r in report_ids)
        deviation_ids = tuple(sorted(
            d.deviation_id for d in self.deviations.values()
            if d.lot_id in chain_lots
        ))
        balance = self.ledger.balance(lot_id).balance_kg
        decision = ReleaseDecision(
            decision_id=self._mid("rel"),
            lot_id=lot_id,
            product=self.ledger.lots[lot_id].product,
            sample_ids=sample_ids,
            report_ids=tuple(report_ids),
            deviation_ids=deviation_ids,
            scope=scope,
            lot_scope=(lot_id,),
            quantity_kg=balance if scope == ReleaseScope.FULL else Decimal(quantity_kg),
            decided_by=user.user_id,
            decided_at=at,
            rationale=rationale,
        )
        self.releases.append(decision)
        self._log(at, user, "release", lot_id,
                  f"{scope.value} 放行 {decision.quantity_kg} kg，"
                  f"报告 {list(report_ids)}，偏差 {list(deviation_ids)}")
        return decision

    def _evaluate_release(self, lot_id: str, report_ids: tuple[str, ...],
                          scope: ReleaseScope, quantity_kg) -> list[str]:
        failures: list[str] = []

        if lot_id not in self.ledger.lots:
            return [f"批号 {lot_id} 不存在"]
        lot = self.ledger.lots[lot_id]
        balance = self.ledger.balance(lot_id).balance_kg
        if balance <= 0:
            failures.append(f"批号 {lot_id} 无成品结存（{balance} kg）")

        # 1) 连续批记录：工序齐全、按序、已签署；曲线缺口须由已关闭偏差覆盖
        chain_lots = self.upstream_lots(lot_id)
        chain = self.record_chain(lot_id)
        required = self._required_steps(lot_id, chain)
        missing = [k.value for k in required if k not in {r.kind for r in chain}]
        if missing:
            failures.append(f"连续批记录缺少工序：{missing}")
        last_order = 0
        for r in chain:
            if r.kind == ProcessKind.SAMPLE:
                continue
            if r.order_no < last_order:
                failures.append(
                    f"记录 {r.record_id}（{r.kind.value}）工序顺序断裂"
                )
            last_order = max(last_order, r.order_no)
            if not r.is_confirmed:
                failures.append(f"工序记录 {r.record_id}（{r.kind.value}）未经操作人签署")
            spec = self.registry.get(r.product, r.specification_version)
            for prev_s, nxt_s, delta in self._gaps(spec, r):
                covers = [
                    d for d in self._record_deviations(r.record_id)
                    if d.status == DeviationStatus.CLOSED
                ]
                if not covers:
                    failures.append(
                        f"炒制记录 {r.record_id} 在 {prev_s}~{nxt_s}s 存在约 "
                        f"{delta // 60} 分钟温度曲线缺口，且无已关闭偏差单覆盖"
                    )

        # 2) 成品批自身必须有包装记录
        if not any(r.lot_id == lot_id and r.kind == ProcessKind.PACK for r in chain):
            failures.append(f"批号 {lot_id} 自身无包装记录，不能作为成品放行")

        # 3) 谱系内偏差全部关闭；本批被判定报废不得放行
        for dev in self.deviations.values():
            if dev.lot_id not in chain_lots:
                continue
            if dev.status != DeviationStatus.CLOSED:
                failures.append(
                    f"批号谱系内偏差 {dev.deviation_id}（{dev.title}）状态为 "
                    f"{dev.status.value}，未关闭"
                )
            if dev.disposition == DeviationStatus.REJECTED and dev.lot_id == lot_id:
                failures.append(f"批号 {lot_id} 已被偏差 {dev.deviation_id} 判定报废")

        # 4) 报告逐张核对样品与批号——严禁借用其他批次报告
        if not report_ids:
            failures.append("放行决定未引用任何检验报告")
        for rid in report_ids:
            report = self.reports.get(rid)
            if report is None:
                failures.append(f"检验报告 {rid} 不存在")
                continue
            if report.lot_id != lot_id:
                raise ReportBorrowingError(
                    f"报告 {rid} 属于批号 {report.lot_id}，不得用于批号 {lot_id} "
                    f"的放行（严禁借用/冒用其他批次报告）"
                )
            if report.invalidated:
                failures.append(f"报告 {rid} 已被复检报告替代/作废")
            elif not report.is_conforming:
                failures.append(f"报告 {rid} 检验结果不合格")
            sample = self.samples.get(report.sample_id)
            if sample is None:
                failures.append(f"报告 {rid} 所引用样品 {report.sample_id} 不存在")
            else:
                if sample.lot_id != lot_id:
                    raise ReportBorrowingError(
                        f"报告 {rid} 的样品 {sample.sample_id} 取自批号 {sample.lot_id}，"
                        f"与放行批 {lot_id} 不符"
                    )
                if sample.status != SampleStatus.TESTED:
                    failures.append(f"样品 {sample.sample_id} 状态 {sample.status.value}")
                record = self.records.get(sample.taken_from_record)
                if record is None or not record.is_confirmed:
                    failures.append(
                        f"样品 {sample.sample_id} 所依据的工序记录未签署"
                    )

        # 5) 返工批必须凭本批新样品/新报告，不得借用原批旧报告
        if lot.rework_of is not None:
            own = [r for r in report_ids if self.reports[r].lot_id == lot_id]
            if not own:
                failures.append(
                    f"返工批 {lot_id} 必须凭本批新样品/新报告放行，不得借用原批 "
                    f"{lot.rework_of} 的旧报告"
                )

        # 6) 部分放行数量不得超过结存
        if scope == ReleaseScope.PARTIAL:
            if quantity_kg is None:
                failures.append("部分放行必须显式给出放行数量")
            elif Decimal(quantity_kg) <= 0 or Decimal(quantity_kg) > balance:
                failures.append(
                    f"部分放行数量 {quantity_kg} kg 超出成品结存 {balance} kg"
                )
        return failures

    def _required_steps(self, lot_id: str,
                        chain: list[ProcessRecord]) -> tuple[ProcessKind, ...]:
        """以批记录引用的规格版本确定必备工序；无记录时取该品种最新登记规格。"""
        product = self.ledger.lots[lot_id].product
        if chain:
            version = chain[0].specification_version
            return self.registry.get(product, version).required_steps
        specs = self.registry._specs.get(product, {})  # noqa: SLF001
        if not specs:
            raise SpecificationError(f"品种 {product} 未登记任何工艺规格")
        latest = max(specs.values(), key=lambda s: s.effective_from)
        return latest.required_steps
