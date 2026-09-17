"""物料台账：逐克守恒的批次谱系。

每一次领料、工序转换、拆分、合批、取样、返工和偏差调账都登记一条
``MaterialMovement``，并满足单条恒等式：

    投入量 = 产出量 + 损耗量 + 取样量

全局审计恒等式：

    累计领料量 = 各批现存数量之和 + 累计损耗 + 累计留样
"""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from .contracts import (
    ZERO,
    LotStatus,
    MaterialLot,
    MaterialMovement,
    MovementKind,
)
from .errors import ConservationError, LedgerError

GRAM = Decimal("0.001")


def _q(value: Decimal) -> Decimal:
    """量化到 1 g，避免长小数造成守恒误差。"""
    return Decimal(value).quantize(GRAM)


class MaterialLedger:
    def __init__(self) -> None:
        self.lots: dict[str, MaterialLot] = {}
        self.movements: list[MaterialMovement] = []
        self._balance: dict[str, Decimal] = {}
        self._samples: dict[str, Decimal] = {}
        self._seq = 0

    # ------------------------------------------------------------------ 基础
    def _new_id(self) -> str:
        self._seq += 1
        return f"mv-{self._seq:04d}"

    @property
    def total_received(self) -> Decimal:
        return sum(
            (m.input_kg for m in self.movements if m.kind == MovementKind.RECEIVE),
            ZERO,
        )

    @property
    def total_loss(self) -> Decimal:
        return sum((m.loss_kg for m in self.movements), ZERO)

    @property
    def total_sampled(self) -> Decimal:
        return sum((m.sample_kg for m in self.movements), ZERO)

    @property
    def total_on_hand(self) -> Decimal:
        return sum(self._balance.values(), ZERO)

    def balance(self, lot_id: str) -> Decimal:
        return self._balance[self._require_lot(lot_id).lot_id]

    def lot(self, lot_id: str) -> MaterialLot:
        return self._require_lot(lot_id)

    def sample_balance(self, sample_id: str) -> Decimal:
        return self._samples.get(sample_id, ZERO)

    def _require_lot(self, lot_id: str) -> MaterialLot:
        try:
            return self.lots[lot_id]
        except KeyError:
            raise LedgerError(f"批号 {lot_id} 不存在") from None

    def _require_active(self, lot_id: str) -> MaterialLot:
        lot = self._require_lot(lot_id)
        if lot.status in (LotStatus.RELEASED, LotStatus.REJECTED):
            raise LedgerError(f"批号 {lot_id} 状态为 {lot.status.value}，不得再移动物料")
        return lot

    def set_status(self, lot_id: str, status: LotStatus) -> None:
        lot = self._require_lot(lot_id)
        self.lots[lot_id] = replace(lot, status=status)

    def _ensure_target(self, lot_id: str, product: str, at: datetime,
                       parents: tuple[str, ...], rework_of: str | None,
                       note: str) -> None:
        if lot_id in self.lots:
            raise LedgerError(f"目标批号 {lot_id} 已存在")
        self.lots[lot_id] = MaterialLot(
            lot_id=lot_id, product=product, quantity_kg=ZERO,
            parent_lots=parents, rework_of=rework_of,
            created_at=at, note=note,
        )
        self._balance[lot_id] = ZERO

    def _check(self, mv: MaterialMovement) -> None:
        """单条守恒与非负校验。"""
        if mv.input_kg != mv.output_kg + mv.loss_kg + mv.sample_kg:
            raise ConservationError(
                f"移动 {mv.movement_id} 不守恒：投入 {mv.input_kg} ≠ "
                f"产出 {mv.output_kg} + 损耗 {mv.loss_kg} + 取样 {mv.sample_kg}"
            )
        for name in ("input_kg", "output_kg", "loss_kg", "sample_kg"):
            if getattr(mv, name) < 0:
                raise ConservationError(f"移动 {mv.movement_id} 出现负数量")

    def _post(self, mv: MaterialMovement) -> MaterialMovement:
        mv = replace(
            mv,
            input_kg=_q(mv.input_kg),
            output_kg=_q(mv.output_kg),
            loss_kg=_q(mv.loss_kg),
            sample_kg=_q(mv.sample_kg),
        )
        self._check(mv)
        if mv.sample_id:
            self._samples[mv.sample_id] = (
                self._samples.get(mv.sample_id, ZERO) + mv.sample_kg
            )
        self.movements.append(mv)
        self._reconcile_or_raise(mv.movement_id)
        return mv

    def _reconcile_or_raise(self, where: str) -> None:
        on_hand, loss = _q(self.total_on_hand), _q(self.total_loss)
        sampled, received = _q(self.total_sampled), _q(self.total_received)
        if on_hand + loss + sampled != received:
            raise ConservationError(
                f"台账全局失衡（{where}）：现存 {on_hand} + 损耗 {loss} "
                f"+ 留样 {sampled} ≠ 领料 {received}"
            )

    # -------------------------------------------------------------- 业务操作
    def receive(self, lot: MaterialLot, at: datetime, note: str = "") -> MaterialMovement:
        if lot.lot_id in self.lots:
            raise LedgerError(f"批号 {lot.lot_id} 已存在")
        qty = _q(lot.quantity_kg)
        self.lots[lot.lot_id] = replace(
            lot, quantity_kg=qty, created_at=lot.created_at or at
        )
        self._balance[lot.lot_id] = qty
        return self._post(MaterialMovement(
            movement_id=self._new_id(),
            source_lots=(), target_lots=(lot.lot_id,),
            input_kg=qty, output_kg=qty, loss_kg=ZERO,
            occurred_at=at, kind=MovementKind.RECEIVE, note=note,
        ))

    def process(self, lot_id: str, output_kg: Decimal, loss_kg: Decimal,
                at: datetime, *, note: str = "",
                deviation_id: str | None = None) -> MaterialMovement:
        """同批工序转换：投入 = 产出 + 损耗，批内数量相应下降。"""
        self._require_active(lot_id)
        output_kg, loss_kg = _q(output_kg), _q(loss_kg)
        input_kg = output_kg + loss_kg
        if self._balance[lot_id] < input_kg:
            raise LedgerError(
                f"{lot_id} 现存 {self._balance[lot_id]} 不足以投入 {input_kg}"
            )
        self._balance[lot_id] -= input_kg - output_kg
        return self._post(MaterialMovement(
            movement_id=self._new_id(),
            source_lots=(lot_id,), target_lots=(lot_id,),
            input_kg=input_kg, output_kg=output_kg, loss_kg=loss_kg,
            occurred_at=at, kind=MovementKind.PROCESS, note=note,
            deviation_id=deviation_id,
        ))

    def split(self, source: str, outputs: dict[str, Decimal], loss_kg: Decimal,
              at: datetime, *, note: str = "") -> MaterialMovement:
        """整批拆分为若干子批，必须整批结清且数量守恒。"""
        self._require_active(source)
        loss_kg = _q(loss_kg)
        total_out = ZERO
        for qty in outputs.values():
            total_out += _q(qty)
        input_kg = total_out + loss_kg
        if self._balance[source] != input_kg:
            raise ConservationError(
                f"拆分必须整批结清：{source} 现存 {self._balance[source]}，"
                f"子批合计 {total_out} + 损耗 {loss_kg} = {input_kg}"
            )
        # 守恒校验通过后再建目标批，避免失败时留下幽灵批号
        for lot_id, qty in list(outputs.items()):
            qty = _q(qty)
            outputs[lot_id] = qty
            self._ensure_target(
                lot_id, self.lots[source].product, at, (source,), None,
                f"由 {source} 拆分",
            )
        self._balance[source] -= input_kg
        for lot_id, qty in outputs.items():
            self._balance[lot_id] += qty
        return self._post(MaterialMovement(
            movement_id=self._new_id(),
            source_lots=(source,), target_lots=tuple(outputs),
            input_kg=input_kg, output_kg=total_out, loss_kg=loss_kg,
            occurred_at=at, kind=MovementKind.SPLIT, note=note,
        ))

    def merge(self, sources: tuple[str, ...] | list[str], target: str,
              loss_kg: Decimal, at: datetime, *, note: str = "") -> MaterialMovement:
        """整批合批：各来源批全部并入新批。"""
        sources = tuple(sources)
        if not sources:
            raise ConservationError("合批必须有来源批")
        for s in sources:
            self._require_active(s)
        product = self.lots[sources[0]].product
        if any(self.lots[s].product != product for s in sources):
            raise LedgerError("只有同品种批次才能合批")
        loss_kg = _q(loss_kg)
        input_kg = _q(sum((self._balance[s] for s in sources), ZERO))
        output_kg = input_kg - loss_kg
        candidate = MaterialMovement(
            movement_id=self._new_id(),
            source_lots=sources, target_lots=(target,),
            input_kg=input_kg, output_kg=output_kg, loss_kg=loss_kg,
            occurred_at=at, kind=MovementKind.MERGE, note=note,
        )
        self._check(candidate)  # 校验在先，避免失败时污染台账
        self._ensure_target(target, product, at, sources, None,
                            f"由 {', '.join(sources)} 合批")
        for s in sources:
            self._balance[s] = ZERO
        self._balance[target] += output_kg
        return self._post(candidate)

    def absorb(self, target: str, excipient_lot: str, quantity_kg: Decimal,
               at: datetime, *, note: str = "") -> MaterialMovement:
        """辅料吸入：将黄酒批的指定数量并入在制批，目标批号不变。"""
        self._require_active(target)
        exc = self._require_active(excipient_lot)
        quantity_kg = _q(quantity_kg)
        if quantity_kg <= 0:
            raise ConservationError("辅料加入量必须为正数")
        if self._balance[excipient_lot] < quantity_kg:
            raise LedgerError(
                f"{excipient_lot} 现存 {self._balance[excipient_lot]} "
                f"不足以投入 {quantity_kg}"
            )
        self._balance[excipient_lot] -= quantity_kg
        self._balance[target] += quantity_kg
        return self._post(MaterialMovement(
            movement_id=self._new_id(),
            source_lots=(excipient_lot,), target_lots=(target,),
            input_kg=quantity_kg, output_kg=quantity_kg, loss_kg=ZERO,
            occurred_at=at, kind=MovementKind.MERGE,
            note=note or f"吸入辅料 {exc.lot_id}（{exc.product}）",
        ))

    def take_sample(self, lot_id: str, sample_id: str, quantity_kg: Decimal,
                    at: datetime, *, note: str = "") -> MaterialMovement:
        """取样留样：从批中扣减并单独追踪，供报告引用核对。"""
        self._require_active(lot_id)
        quantity_kg = _q(quantity_kg)
        if quantity_kg <= 0:
            raise ConservationError("取样量必须为正数")
        if self._balance[lot_id] < quantity_kg:
            raise LedgerError(
                f"{lot_id} 现存 {self._balance[lot_id]} 不足以取样 {quantity_kg}"
            )
        self._balance[lot_id] -= quantity_kg
        return self._post(MaterialMovement(
            movement_id=self._new_id(),
            source_lots=(lot_id,), target_lots=(),
            input_kg=quantity_kg, output_kg=ZERO, loss_kg=ZERO,
            sample_kg=quantity_kg, sample_id=sample_id,
            occurred_at=at, kind=MovementKind.SAMPLE, note=note,
        ))

    def rework(self, source: str, target: str, output_kg: Decimal, loss_kg: Decimal,
               at: datetime, *, note: str = "") -> MaterialMovement:
        """返工：原批隔离，物料转入关联新批（rework_of 指回原批）。"""
        src_lot = self._require_active(source)
        output_kg, loss_kg = _q(output_kg), _q(loss_kg)
        input_kg = output_kg + loss_kg
        if self._balance[source] < input_kg:
            raise LedgerError(
                f"返工投入 {input_kg} 超过 {source} 现存 {self._balance[source]}"
            )
        self._ensure_target(target, src_lot.product, at, (source,), source,
                            note or f"返工自 {source}")
        self._balance[source] -= input_kg
        self._balance[target] += output_kg
        self.set_status(source, LotStatus.QUARANTINED)
        return self._post(MaterialMovement(
            movement_id=self._new_id(),
            source_lots=(source,), target_lots=(target,),
            input_kg=input_kg, output_kg=output_kg, loss_kg=loss_kg,
            occurred_at=at, kind=MovementKind.REWORK, note=note,
        ))

    def adjust(self, lot_id: str, loss_kg: Decimal, at: datetime,
               deviation_id: str, *, note: str = "") -> MaterialMovement:
        """偏差核定后的数量调账：只减不增，必须挂偏差单。"""
        self._require_active(lot_id)
        loss_kg = _q(loss_kg)
        if loss_kg <= 0:
            raise ConservationError("调账核减必须为正数")
        if self._balance[lot_id] < loss_kg:
            raise LedgerError(
                f"{lot_id} 现存 {self._balance[lot_id]} 不足以核减 {loss_kg}"
            )
        self._balance[lot_id] -= loss_kg
        return self._post(MaterialMovement(
            movement_id=self._new_id(),
            source_lots=(lot_id,), target_lots=(),
            input_kg=loss_kg, output_kg=ZERO, loss_kg=loss_kg,
            occurred_at=at, kind=MovementKind.ADJUST, note=note,
            deviation_id=deviation_id,
        ))

    # ------------------------------------------------------------------ 审计
    def movements_of(self, lot_id: str) -> list[MaterialMovement]:
        return [
            m for m in self.movements
            if lot_id in m.source_lots or lot_id in m.target_lots
        ]

    def lineage(self, lot_id: str) -> dict[str, object]:
        """返回一批的前后谱系、返工关联与现存数量。"""
        lot = self._require_lot(lot_id)
        children = [
            t for m in self.movements if lot_id in m.source_lots
            for t in m.target_lots if t != lot_id
        ]
        return {
            "lot_id": lot_id,
            "product": lot.product,
            "status": lot.status,
            "balance_kg": self._balance[lot_id],
            "parents": list(lot.parent_lots),
            "rework_of": lot.rework_of,
            "children": children,
        }

    def reconciliation(self) -> dict[str, Decimal]:
        return {
            "received_kg": _q(self.total_received),
            "on_hand_kg": _q(self.total_on_hand),
            "loss_kg": _q(self.total_loss),
            "sampled_kg": _q(self.total_sampled),
        }
