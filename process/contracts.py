"""物料谱系、工艺记录与放行决定。

所有质量字段使用 ``Decimal``，杜绝浮点误差；时间统一为 naive UTC 或带时区
datetime（平台不做时区换算，按录入值比较）。

关键不变量
----------
* 任何 :class:`MaterialMovement` 都满足 ``输入 = 产出之和 + 损耗``。
* 工序参数只按 *生产品种 + 规格生效版本* 校验（见 ``specs.py``）。
* 传感器补传只能写入 *未签署* 的工序记录；已签署曲线只能通过偏差单修正。
* 返工产生新批号，并经 ``rework_of`` 关联原批，不得借用原批检验报告。
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
class Role(StrEnum):
    OPERATOR = "operator"        # 操作人：执行工序、签署本人记录
    QA = "qa"                    # 现场 QA：取样、发起复检
    DEVIATION_OWNER = "deviation_owner"  # 偏差管理员：登记/评估偏差
    REWORK_PLANNER = "rework_planner"    # 工艺员：批准返工
    QP = "qp"                    # 质量受权人：最终放行决定


# ---------------------------------------------------------------------------
# 工序与物料
# ---------------------------------------------------------------------------
class ProcessKind(StrEnum):
    CLEAN = "clean"        # 净制
    CUT = "cut"            # 切制
    ADD_EXCIPIENT = "add_excipient"  # 辅料加入（酒炙：黄酒）
    MOISTEN = "moisten"    # 润制
    FRY = "fry"            # 炒制
    DRY = "dry"            # 干燥
    SAMPLE = "sample"      # 取样（不改变加工状态，仅产生样品）
    PACK = "pack"          # 包装


# 按品种规定的工序先后；取样可发生在包装前任意工序之后。
PROCESS_ORDER: dict[ProcessKind, int] = {
    ProcessKind.CLEAN: 10,
    ProcessKind.CUT: 20,
    ProcessKind.ADD_EXCIPIENT: 30,
    ProcessKind.MOISTEN: 40,
    ProcessKind.FRY: 50,
    ProcessKind.DRY: 60,
    ProcessKind.SAMPLE: 70,
    ProcessKind.PACK: 80,
}


class MovementKind(StrEnum):
    ISSUE = "issue"      # 领料投料到批
    SPLIT = "split"      # 拆批
    MERGE = "merge"      # 合批
    CONSUME = "consume"  # 工序加工（重量随水分/辅料变化，差额记损耗或增重）
    REWORK = "rework"    # 返工：旧批转入新批
    SCRAP = "scrap"      # 报废
    SAMPLE = "sample"    # 取样移出
    PACK = "pack"        # 包装产出成品


@dataclass(frozen=True)
class MaterialLot:
    """一批物料（原料、中间体、辅料或成品）。"""

    lot_id: str
    product: str                  # 生产品种，如“酒炙当归”；辅料可为“黄酒”
    initial_kg: Decimal           # 本批最初建账量（领料登记时确定）
    is_excipient: bool = False
    created_at: datetime | None = None
    rework_of: str | None = None  # 返工批号 -> 原批号；None 表示非返工批


@dataclass(frozen=True)
class MaterialMovement:
    """一次物料移动。守恒式：``sum(各来源分量) == sum(各目标分量) + 损耗``。

    辅料加入等允许 *增重*：此时约定损耗为负（=净增重），守恒式不变，
    因此用有符号的 ``loss_kg``，台账按批分别维护正损耗与负增重列。

    ``source_amounts`` / ``target_amounts`` 与来源/目标批号一一对应，给出
    每一批的实际扣减/增加量，使拆分与合批可以逐克核对；缺省为 None 时
    仅允许单来源/单目标，由台账以总量填充。
    """

    movement_id: str
    kind: MovementKind
    source_lots: tuple[str, ...]
    target_lots: tuple[str, ...]
    input_kg: Decimal
    output_kg: Decimal
    loss_kg: Decimal
    occurred_at: datetime
    source_amounts: tuple[Decimal, ...] | None = None
    target_amounts: tuple[Decimal, ...] | None = None
    loss_on: tuple[str, ...] = ()  # 损耗承担批号；默认落在来源批
    loss_amounts: tuple[Decimal, ...] | None = None  # 与 loss_on 一一对应
    ref: str | None = None  # 关联工序记录/偏差单/取样单


@dataclass(frozen=True)
class LotBalance:
    """单个批号的实时台账：累计投入、累计产出、累计损耗、当前结余。"""

    lot_id: str
    total_in_kg: Decimal
    total_out_kg: Decimal
    loss_kg: Decimal       # 正损耗（水分散失、拣杂、取样等）
    gain_kg: Decimal       # 负损耗（辅料带入）
    balance_kg: Decimal    # = 初始 + 总入 - 总出 - 净损耗

    @property
    def net_loss_kg(self) -> Decimal:
        return self.loss_kg - self.gain_kg


# ---------------------------------------------------------------------------
# 传感器曲线与工序记录
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CurvePoint:
    """温度（℃）随炒制时间（秒）的采样点。"""

    t_seconds: int
    temp_c: Decimal


@dataclass(frozen=True)
class SensorBackfill:
    """传感器补传凭据：补传只允许进入未签署记录。"""

    payload_digest: str
    received_at: datetime
    gap_seconds: tuple[int, int] | None  # 所填补的缺口区间（起,止）


@dataclass(frozen=True)
class ProcessRecord:
    """连续批记录中的一道工序。"""

    record_id: str
    lot_id: str
    kind: ProcessKind
    product: str
    specification_version: str          # 本批执行的工艺规格生效版本
    order_no: int                        # 来自 PROCESS_ORDER，保证连续顺序
    started_at: datetime
    ended_at: datetime | None
    parameters: dict[str, str]           # 规格参数的实测值（字符串化）
    curve: tuple[CurvePoint, ...] = ()
    sensor_digest: str | None = None     # 原始采集报文摘要
    backfill: SensorBackfill | None = None
    confirmed_by: str | None = None      # 操作人工号；签署后曲线冻结
    confirmed_at: datetime | None = None
    deviation_id: str | None = None      # 已签署曲线被修正时，必须挂偏差单

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_by is not None


# ---------------------------------------------------------------------------
# 取样、检验与报告
# ---------------------------------------------------------------------------
class SampleStatus(StrEnum):
    DRAWN = "drawn"          # 已取样
    TESTING = "testing"
    TESTED = "tested"        # 已出具报告
    INVALIDATED = "invalidated"  # 偏差复检后作废


@dataclass(frozen=True)
class Sample:
    sample_id: str
    lot_id: str
    taken_from_record: str   # 取样所跟的工序记录（证明样品代表该批状态）
    quantity_kg: Decimal
    drawn_by: str
    drawn_at: datetime
    status: SampleStatus
    report_id: str | None = None


@dataclass(frozen=True)
class TestReport:
    """成品（或中间体质控）检验报告。报告只对其样品与批号负责。"""

    report_id: str
    sample_id: str
    lot_id: str
    product: str
    specification_version: str
    issued_at: datetime
    results: dict[str, Decimal]      # 检验项 -> 实测值
    limits: dict[str, tuple[Decimal, Decimal]]  # 检验项 -> (下限,上限)
    supersedes: str | None = None    # 复检报告指向被替代报告
    invalidated: bool = False

    @property
    def is_conforming(self) -> bool:
        if self.invalidated:
            return False
        return all(
            lo <= value <= hi for (lo, hi), value in (
                (self.limits[name], self.results[name]) for name in self.results
            )
        )


# ---------------------------------------------------------------------------
# 偏差
# ---------------------------------------------------------------------------
class DeviationKind(StrEnum):
    TEMPERATURE_GAP = "temperature-gap"  # 炒制温度曲线缺口
    YIELD_MISMATCH = "yield-mismatch"    # 成品收率与领料量对不上
    PARAMETER_EXCURSION = "parameter-excursion"
    OTHER = "other"


class DeviationStatus(StrEnum):
    OPEN = "open"            # 已登记
    ASSESSED = "assessed"    # QA/偏差管理员完成评估
    REWORK_APPROVED = "rework_approved"  # 评估结论：返工
    RETEST_APPROVED = "retest_approved"  # 评估结论：补采复检
    CONCESSION_APPROVED = "concession_approved"  # 让步接收
    REJECTED = "rejected"
    CLOSED = "closed"        # 纠正措施（返工/复检）完成后关闭


# 偏差评估结论集合
ASSESSED_DISPOSITIONS = {
    DeviationStatus.REWORK_APPROVED,
    DeviationStatus.RETEST_APPROVED,
    DeviationStatus.CONCESSION_APPROVED,
    DeviationStatus.REJECTED,
}


@dataclass(frozen=True)
class Deviation:
    deviation_id: str
    lot_id: str
    kind: DeviationKind
    title: str
    detail: str
    opened_by: str
    opened_at: datetime
    status: DeviationStatus
    assessed_by: str | None = None
    assessed_at: datetime | None = None
    assessment: str | None = None
    disposition: DeviationStatus | None = None
    closure_note: str | None = None
    closed_by: str | None = None
    closed_at: datetime | None = None
    linked_record_id: str | None = None   # 被修正的工序记录
    linked_report_id: str | None = None   # 关联的复检报告


# ---------------------------------------------------------------------------
# 返工
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReworkOrder:
    rework_id: str
    original_lot: str
    rework_lot: str
    deviation_id: str
    product: str
    specification_version: str
    planned_by: str
    planned_at: datetime
    completed: bool = False
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# 放行
# ---------------------------------------------------------------------------
class ReleaseScope(StrEnum):
    FULL = "full"             # 整批放行
    PARTIAL = "partial"       # 限定批号/数量范围放行
    REJECTED = "rejected"     # 不放行


@dataclass(frozen=True)
class ReleaseDecision:
    decision_id: str
    lot_id: str
    product: str
    sample_ids: tuple[str, ...]
    report_ids: tuple[str, ...]
    deviation_ids: tuple[str, ...]
    scope: ReleaseScope
    lot_scope: tuple[str, ...]   # 实际覆盖的批号（部分放行时为子集）
    quantity_kg: Decimal | None  # 放行数量（部分放行时显式给出）
    decided_by: str
    decided_at: datetime
    rationale: str = ""
