"""物料谱系、工艺记录与放行决定。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class ProcessKind(StrEnum):
    CLEAN = "clean"
    CUT = "cut"
    MOISTEN = "moisten"
    FRY = "fry"
    DRY = "dry"
    SAMPLE = "sample"
    PACK = "pack"


@dataclass(frozen=True)
class MaterialMovement:
    movement_id: str
    source_lots: tuple[str, ...]
    target_lots: tuple[str, ...]
    input_kg: Decimal
    output_kg: Decimal
    loss_kg: Decimal
    occurred_at: datetime


@dataclass(frozen=True)
class ProcessRecord:
    record_id: str
    lot_id: str
    kind: ProcessKind
    specification_version: str
    started_at: datetime
    ended_at: datetime | None
    sensor_digest: str | None
    confirmed_by: str | None


@dataclass(frozen=True)
class ReleaseDecision:
    decision_id: str
    lot_id: str
    sample_ids: tuple[str, ...]
    deviation_ids: tuple[str, ...]
    scope: str
    decided_by: str
    decided_at: datetime
