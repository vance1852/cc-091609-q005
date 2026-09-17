"""物料谱系、工艺记录与放行决定。

本模块只定义领域契约（数据结构与枚举），不包含业务规则。
所有数量一律使用 ``Decimal``，以千克为单位、保留三位小数（即 1 g）。
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

ZERO = Decimal("0")


class ProcessKind(StrEnum):
    """酒炙当归连续批记录的工步。"""

    CLEAN = "clean"                    # 净制
    CUT = "cut"                        # 切制
    ADD_EXCIPIENT = "add_excipient"    # 辅料加入（黄酒）
    MOISTEN = "moisten"                # 润制
    FRY = "fry"                        # 炒制
    DRY = "dry"                        # 干燥
    SAMPLE = "sample"                  # 取样
    PACK = "pack"                      # 包装


class MovementKind(StrEnum):
    """物料移动类型。"""

    RECEIVE = "receive"    # 领料入库
    PROCESS = "process"    # 工序转换（同批加工，产生损耗）
    SPLIT = "split"        # 拆分
    MERGE = "merge"        # 合批（含辅料吸入同一批）
    REWORK = "rework"      # 返工：原批挂起，产生关联新批
    ADJUST = "adjust"      # 偏差核定后的账目调整
    SAMPLE = "sample"      # 取样留样（从批中扣减，单独追踪）


class LotStatus(StrEnum):
    IN_PROCESS = "in_process"    # 在制/待验
    QUARANTINED = "quarantined"  # 隔离（偏差待处理或已转返工）
    RELEASED = "released"        # 已放行
    REJECTED = "rejected"        # 已拒绝放行


class RecordStatus(StrEnum):
    OPEN = "open"                # 未签署，传感器尚可补传
    CONFIRMED = "confirmed"      # 操作人已确认，曲线冻结
    CORRECTED = "corrected"      # 已被偏差修正记录替代


class DeviationStatus(StrEnum):
    OPEN = "open"
    ASSESSED = "assessed"        # QA 已评估并给出处置
    VERIFIED = "verified"        # 处置措施已执行并经 QA 验证
    CLOSED = "closed"            # 关闭，可支持放行


class DispositionKind(StrEnum):
    RETEST = "retest"            # 复检
    REWORK = "rework"            # 返工
    ADJUST = "adjust"            # 数量账目调整
    CORRECT = "correct"          # 修正已签署记录
    USE_AS_IS = "use_as_is"      # 让步接收
    REJECT = "reject"            # 判废


class SampleKind(StrEnum):
    ROUTINE = "routine"          # 常规取样
    SUPPLEMENT = "supplement"    # 补采（返工后重新取样）
    RETEST = "retest"            # 复检取样


class Role(StrEnum):
    OPERATOR = "operator"  # 操作人员：操作、确认记录、执行返工
    SENSOR = "sensor"      # 采集网关：传感器补传
    QC = "qc"              # 检验人员：取样、检验、出报告
    QA = "qa"              # 质量管理人员：偏差评估、验证、关闭
    QP = "qp"              # 质量受权人：最终放行


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    role: Role


@dataclass(frozen=True)
class MaterialLot:
    """一批物料（原料、辅料、中间产品或成品批）。"""

    lot_id: str
    product: str
    quantity_kg: Decimal
    status: LotStatus = LotStatus.IN_PROCESS
    parent_lots: tuple[str, ...] = ()
    rework_of: str | None = None
    note: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True)
class MaterialMovement:
    """一次物料移动。恒等式：input_kg = output_kg + loss_kg + sample_kg。"""

    movement_id: str
    source_lots: tuple[str, ...]
    target_lots: tuple[str, ...]
    input_kg: Decimal
    output_kg: Decimal
    loss_kg: Decimal
    occurred_at: datetime
    kind: MovementKind = MovementKind.PROCESS
    sample_kg: Decimal = ZERO
    sample_id: str | None = None
    note: str = ""
    deviation_id: str | None = None


@dataclass(frozen=True)
class CurvePoint:
    at: datetime
    temp_c: Decimal


@dataclass(frozen=True)
class ProcessRecord:
    """一道工序的批记录。

    曲线与参数在 ``status == OPEN`` 时可由传感器补传；
    一旦操作人确认（CONFIRMED）即冻结，只能凭偏差单出具修正记录。
    """

    record_id: str
    lot_id: str
    kind: ProcessKind
    specification_version: str
    started_at: datetime
    ended_at: datetime | None
    sensor_digest: str | None
    confirmed_by: str | None
    product: str = ""
    parameters: dict[str, Decimal] = field(default_factory=dict)
    curve: tuple[CurvePoint, ...] = ()
    status: RecordStatus = RecordStatus.OPEN
    signed_at: datetime | None = None
    deviation_id: str | None = None
    supersedes: str | None = None


@dataclass(frozen=True)
class Deviation:
    deviation_id: str
    lot_id: str
    kind: str
    title: str
    raised_by: str
    raised_at: datetime
    status: DeviationStatus = DeviationStatus.OPEN
    detail: str = ""
    root_cause: str = ""
    impacted_records: tuple[str, ...] = ()
    disposition: DispositionKind | None = None
    assessed_by: str | None = None
    assessed_at: datetime | None = None
    evidence: tuple[str, ...] = ()
    verified_by: str | None = None
    verified_at: datetime | None = None
    closed_by: str | None = None
    closed_at: datetime | None = None


@dataclass(frozen=True)
class Sample:
    sample_id: str
    lot_id: str
    quantity_kg: Decimal
    taken_at: datetime
    taken_by: str
    kind: SampleKind = SampleKind.ROUTINE
    note: str = ""


@dataclass(frozen=True)
class TestResult:
    test_name: str
    value: Decimal
    lower: Decimal | None
    upper: Decimal | None
    unit: str
    method: str = ""

    @property
    def conforms(self) -> bool:
        if self.lower is not None and self.value < self.lower:
            return False
        if self.upper is not None and self.value > self.upper:
            return False
        return True


@dataclass(frozen=True)
class InspectionReport:
    """检验报告只对其签发批次和所列样品负责，不得跨批借用。"""

    report_id: str
    lot_id: str
    sample_ids: tuple[str, ...]
    issued_by: str
    issued_at: datetime
    results: tuple[TestResult, ...]
    specification_version: str
    external: bool = False

    @property
    def conforms(self) -> bool:
        return bool(self.results) and all(r.conforms for r in self.results)


@dataclass(frozen=True)
class ReleaseDecision:
    decision_id: str
    lot_id: str
    sample_ids: tuple[str, ...]
    deviation_ids: tuple[str, ...]
    scope: str
    decided_by: str
    decided_at: datetime
    product: str = ""
    report_id: str | None = None
    approved: bool = False
    quantity_kg: Decimal = ZERO
    rejected_reasons: tuple[str, ...] = ()
