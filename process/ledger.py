"""物料台账：批号建账与守恒移动。

守恒规则（每次移动独立校验）::

    全局：input_kg == output_kg + loss_kg
    来源批合计：转出(out)之和 + 分摊损耗 == input_kg
    目标批合计：转入(in)之和 == output_kg

每个批号维护三列独立账目（``in`` / ``out`` / ``loss``），故同一批号既转出又
转入时（如药材批接收辅料）不会相互抵消：

* ``loss`` 为正表示损耗（水分散失、拣杂、取样、报废）；同批加工允许为负，
  表示增重（此时没有批间转移）。
* 多来源合批必须显式给出 ``source_amounts`` 与损耗分摊；单来源移动由台账填充。
* 返工批建账时必须以 ``rework_of`` 指向原批。

台账可按批号回放每一笔过账，做到每一克物料去向可核对。
"""

from dataclasses import dataclass, field
from decimal import Decimal

from .contracts import LotBalance, MaterialLot, MaterialMovement, MovementKind
from .errors import ConservationError

ZERO = Decimal("0")
Q = Decimal("0.001")  # 台账精度：克


def _q(value: Decimal) -> Decimal:
    return value.quantize(Q)


@dataclass
class Ledger:
    lots: dict[str, MaterialLot] = field(default_factory=dict)
    movements: list[MaterialMovement] = field(default_factory=list)
    # lot_id -> [(movement_id, 转入, 转出, 带符号损耗)]
    _posts: dict[str, list[tuple[str, Decimal, Decimal, Decimal]]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 批号建账
    # ------------------------------------------------------------------
    def open_lot(
        self,
        lot_id: str,
        product: str,
        initial_kg: Decimal,
        *,
        created_at=None,
        is_excipient: bool = False,
        rework_of: str | None = None,
    ) -> MaterialLot:
        if lot_id in self.lots:
            raise ConservationError(f"批号 {lot_id} 已存在，不得重复建账")
        if initial_kg < 0:
            raise ConservationError(f"批号 {lot_id} 初始量不得为负")
        if rework_of is not None and rework_of not in self.lots:
            raise ConservationError(
                f"返工批 {lot_id} 的原批号 {rework_of} 不存在"
            )
        lot = MaterialLot(
            lot_id=lot_id,
            product=product,
            initial_kg=_q(initial_kg),
            is_excipient=is_excipient,
            created_at=created_at,
            rework_of=rework_of,
        )
        self.lots[lot_id] = lot
        self._posts[lot_id] = []
        return lot

    # ------------------------------------------------------------------
    # 移动记账
    # ------------------------------------------------------------------
    def post(self, movement: MaterialMovement) -> None:
        for lot_id in (*movement.source_lots, *movement.target_lots):
            if lot_id not in self.lots:
                raise ConservationError(
                    f"移动 {movement.movement_id} 引用了不存在的批号 {lot_id}"
                )
        plan = self._validate(movement)
        self._apply(movement, plan)
        self.movements.append(movement)

    def _validate(self, m: MaterialMovement) -> dict[str, dict[str, Decimal]]:
        """校验并返回过账计划：{lot: {in, out, loss}}，三列各自非负（loss 可负）。"""
        if any(x.movement_id == m.movement_id for x in self.movements):
            raise ConservationError(f"移动单号 {m.movement_id} 重复")
        if m.input_kg < 0 or m.output_kg < 0:
            raise ConservationError(f"移动 {m.movement_id} 投入/产出不得为负")
        if _q(m.input_kg - m.output_kg) != _q(m.loss_kg):
            raise ConservationError(
                f"移动 {m.movement_id}（{m.kind.value}）数量不守恒："
                f"投入 {m.input_kg} ≠ 产出 {m.output_kg} + 损耗 {m.loss_kg}"
            )

        sources = m.source_lots
        targets = m.target_lots
        plan: dict[str, dict[str, Decimal]] = {
            lot: {"in": ZERO, "out": ZERO, "loss": ZERO}
            for lot in (*sources, *targets)
        }

        # --- 领料入账：初始量在建账时登记，此处仅留存凭证 ---
        if m.kind == MovementKind.ISSUE:
            if sources:
                raise ConservationError("领料移动不应有来源批")
            if _q(m.input_kg) != _q(
                sum((self.lots[t].initial_kg for t in targets), ZERO)
            ):
                raise ConservationError(
                    f"领料 {m.movement_id} 入账量与批号建账量不一致"
                )
            return plan

        if not sources:
            raise ConservationError(f"移动 {m.movement_id} 缺少来源批")

        # --- 纯移出损耗：报废 / 取样（来源批 out 列记整笔投入）---
        if m.kind in (MovementKind.SCRAP, MovementKind.SAMPLE):
            if targets:
                raise ConservationError(f"移动 {m.movement_id} 不应有产出批")
            alloc = self._allocate(m, sources, m.input_kg)
            for lot, amount in alloc.items():
                plan[lot]["out"] += amount
            return plan

        if not targets:
            raise ConservationError(f"移动 {m.movement_id} 缺少产出批")

        # --- 同批加工：物料不转移，仅以有符号损耗改变结存 ---
        if m.kind == MovementKind.CONSUME:
            if len(sources) != 1 or tuple(targets) != tuple(sources):
                raise ConservationError("加工移动必须是同批投入同批产出")
            plan[sources[0]]["loss"] += m.loss_kg
            return plan

        # --- 批间转移：split / merge / rework ---
        if m.kind == MovementKind.REWORK:
            if len(sources) != 1 or len(targets) != 1:
                raise ConservationError("返工移动必须为单一原批 -> 单一返工批")
            if self.lots[targets[0]].rework_of != sources[0]:
                raise ConservationError(
                    f"返工批 {targets[0]} 未通过 rework_of 关联原批 {sources[0]}"
                )
        if m.loss_kg < 0:
            raise ConservationError(
                f"移动 {m.movement_id} 批间转移不得出现负损耗（增重应走同批加工）"
            )

        # 目标转入
        target_amounts = self._amounts(m, targets, m.target_amounts, "目标")
        if target_amounts is None:
            if len(targets) != 1:
                raise ConservationError(
                    f"移动 {m.movement_id} 多目标必须显式给出 target_amounts"
                )
            target_amounts = {targets[0]: m.output_kg}
        if _q(sum(target_amounts.values(), ZERO)) != _q(m.output_kg):
            raise ConservationError(
                f"移动 {m.movement_id} 目标转入分量之和 "
                f"{sum(target_amounts.values(), ZERO)} ≠ 产出 {m.output_kg}"
            )
        for lot, amount in target_amounts.items():
            plan[lot]["in"] += amount

        # 来源转出（= 产出在各来源的分量，不含损耗）
        source_amounts = self._amounts(m, sources, m.source_amounts, "来源")
        if source_amounts is None:
            if len(sources) != 1:
                raise ConservationError(
                    f"移动 {m.movement_id} 多来源合批必须显式给出 source_amounts"
                )
            source_amounts = {sources[0]: m.output_kg}
        if _q(sum(source_amounts.values(), ZERO)) != _q(m.output_kg):
            raise ConservationError(
                f"移动 {m.movement_id} 来源转出分量之和 "
                f"{sum(source_amounts.values(), ZERO)} ≠ 产出 {m.output_kg}"
            )
        for lot, amount in source_amounts.items():
            plan[lot]["out"] += amount

        # 损耗分摊（落在来源批）
        if m.loss_kg:
            loss_alloc = self._allocate_loss(m, sources, m.loss_kg)
            for lot, amount in loss_alloc.items():
                plan[lot]["loss"] += amount

        # 来源实际扣减 = 转出 + 损耗，必须等于投入
        withdrawn = sum(
            (plan[lot]["out"] + plan[lot]["loss"] for lot in sources), ZERO
        )
        if _q(withdrawn) != _q(m.input_kg):
            raise ConservationError(
                f"移动 {m.movement_id} 各来源实际扣减 {withdrawn} ≠ 投入 {m.input_kg}"
            )
        return plan

    def _amounts(
        self,
        m: MaterialMovement,
        lots: tuple[str, ...],
        amounts: tuple[Decimal, ...] | None,
        label: str,
    ) -> dict[str, Decimal] | None:
        if amounts is None:
            return None
        if len(amounts) != len(lots):
            raise ConservationError(
                f"移动 {m.movement_id} {label}分量数与批号数不一致"
            )
        return dict(zip(lots, amounts))

    def _allocate(
        self, m: MaterialMovement, lots: tuple[str, ...], total: Decimal
    ) -> dict[str, Decimal]:
        """纯移出量在来源批间分摊：显式 loss_on/loss_amounts 优先，单来源默认全担。"""
        bearers = m.loss_on or lots
        explicit = m.loss_amounts
        if explicit is not None:
            if len(explicit) != len(bearers):
                raise ConservationError(
                    f"移动 {m.movement_id} 承担批号与分摊量数量不一致"
                )
            alloc = dict(zip(bearers, explicit))
            if set(bearers) - set(lots):
                raise ConservationError(
                    f"移动 {m.movement_id} 承担批号 {set(bearers) - set(lots)} 不在来源中"
                )
            if _q(sum(alloc.values(), ZERO)) != _q(total):
                raise ConservationError(
                    f"移动 {m.movement_id} 分摊之和 {sum(alloc.values(), ZERO)} "
                    f"≠ {total}"
                )
            return alloc
        if len(lots) == 1:
            return {lots[0]: total}
        raise ConservationError(
            f"移动 {m.movement_id} 涉及多个来源批，必须显式给出分摊"
            f"（loss_on/loss_amounts）"
        )

    def _allocate_loss(
        self, m: MaterialMovement, lots: tuple[str, ...], total: Decimal
    ) -> dict[str, Decimal]:
        return self._allocate(m, lots, total)

    def _apply(
        self, m: MaterialMovement, plan: dict[str, dict[str, Decimal]]
    ) -> None:
        for lot, item in plan.items():
            self._posts[lot].append(
                (m.movement_id, _q(item["in"]), _q(item["out"]), _q(item["loss"]))
            )
        for lot in (*m.source_lots, *m.target_lots):
            if self.balance(lot).balance_kg < 0:
                raise ConservationError(
                    f"移动 {m.movement_id} 过账后批号 {lot} 结存为负，库存不足"
                )

    # ------------------------------------------------------------------
    # 余额与回放
    # ------------------------------------------------------------------
    def balance(self, lot_id: str) -> LotBalance:
        lot = self.lots[lot_id]
        total_in = ZERO
        total_out = ZERO
        loss = ZERO
        gain = ZERO
        for _mid, in_kg, out_kg, lot_loss in self._posts[lot_id]:
            total_in += in_kg
            total_out += out_kg
            if lot_loss > 0:
                loss += lot_loss
            elif lot_loss < 0:
                gain += -lot_loss
        balance = lot.initial_kg + total_in - total_out - (loss - gain)
        return LotBalance(
            lot_id=lot_id,
            total_in_kg=_q(total_in),
            total_out_kg=_q(total_out),
            loss_kg=_q(loss),
            gain_kg=_q(gain),
            balance_kg=_q(balance),
        )

    def trace(self, lot_id: str) -> list[dict]:
        """回放某批号全部过账明细，供逐克核对。"""
        result = [
            {
                "movement_id": "OPEN",
                "in_kg": self.lots[lot_id].initial_kg,
                "out_kg": ZERO,
                "loss_kg": ZERO,
                "note": "建账/领料",
            }
        ]
        for mid, in_kg, out_kg, lot_loss in self._posts[lot_id]:
            if in_kg == 0 and out_kg == 0 and lot_loss == 0:
                continue
            result.append(
                {"movement_id": mid, "in_kg": in_kg, "out_kg": out_kg,
                 "loss_kg": lot_loss}
            )
        result.append(
            {
                "movement_id": "BALANCE",
                "in_kg": self.balance(lot_id).balance_kg,
                "out_kg": ZERO,
                "loss_kg": ZERO,
                "note": "当前结存",
            }
        )
        return result

    def movement(self, movement_id: str) -> MaterialMovement:
        for m in self.movements:
            if m.movement_id == movement_id:
                return m
        raise KeyError(movement_id)

    def global_conservation(self) -> Decimal:
        """所有批号当前结存之和，供总平核对。"""
        return _q(sum((self.balance(lot).balance_kg for lot in self.lots), ZERO))
