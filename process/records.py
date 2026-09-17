"""连续批记录的登记、补传、签署与修正。

规则：

* 记录处于 ``OPEN`` 时，采集网关可以补传曲线/参数（填补十分钟缺口）；
* 操作人确认后记录变为 ``CONFIRMED``，曲线与参数冻结，任何人不得改写；
* 已确认记录只能凭偏差单出具一条新的修正记录（``supersedes`` 指向原记录），
  原记录置为 ``CORRECTED`` 留痕，原始数据永不物理覆盖。
"""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from .contracts import (
    CurvePoint,
    ProcessKind,
    ProcessRecord,
    RecordStatus,
    Role,
    User,
)
from .errors import RecordError
from .specifications import SpecificationRegistry


class RecordBook:
    def __init__(self, specs: SpecificationRegistry) -> None:
        self.specs = specs
        self.records: dict[str, ProcessRecord] = {}
        self._seq = 0

    def _rid(self) -> str:
        self._seq += 1
        return f"rec-{self._seq:04d}"

    def open_record(
        self, *, lot_id: str, kind: ProcessKind, product: str,
        specification_version: str, started_at: datetime,
        ended_at: datetime | None = None,
        parameters: dict[str, Decimal] | None = None,
        curve: tuple[CurvePoint, ...] | list[CurvePoint] = (),
        sensor_digest: str | None = None,
    ) -> ProcessRecord:
        rec = ProcessRecord(
            record_id=self._rid(), lot_id=lot_id, kind=kind, product=product,
            specification_version=specification_version,
            started_at=started_at, ended_at=ended_at,
            parameters=dict(parameters or {}),
            curve=tuple(curve), sensor_digest=sensor_digest,
            confirmed_by=None, status=RecordStatus.OPEN,
        )
        self.records[rec.record_id] = rec
        return rec

    def _get(self, record_id: str) -> ProcessRecord:
        try:
            return self.records[record_id]
        except KeyError:
            raise RecordError(f"工序记录 {record_id} 不存在") from None

    def backfill(
        self, record_id: str, actor: User, points: list[CurvePoint],
        *, digest: str, parameters: dict[str, Decimal] | None = None,
        ended_at: datetime | None = None,
    ) -> ProcessRecord:
        """传感器补传：仅采集网关可对未签署记录追加/合并曲线点。"""
        if actor.role is not Role.SENSOR:
            raise RecordError(f"角色 {actor.role.value} 不得补传传感器数据")
        rec = self._get(record_id)
        if rec.status is not RecordStatus.OPEN:
            raise RecordError(
                f"记录 {record_id} 已{rec.status.value}，"
                "已确认曲线只能凭偏差单修正，不得补传"
            )
        merged = {p.at: p for p in rec.curve}
        for p in points:
            merged[p.at] = p
        new_params = {**rec.parameters, **(parameters or {})}
        rec = replace(
            rec,
            curve=tuple(sorted(merged.values(), key=lambda p: p.at)),
            parameters=new_params,
            sensor_digest=digest,
            ended_at=ended_at or rec.ended_at,
        )
        self.records[record_id] = rec
        return rec

    def confirm(
        self, record_id: str, actor: User, at: datetime,
        *, require_spec_ok: bool = True,
    ) -> ProcessRecord:
        """操作人确认签署。签署前工艺参数/曲线须合规（可被偏差放行覆盖）。"""
        if actor.role is not Role.OPERATOR:
            raise RecordError(f"角色 {actor.role.value} 不得签署工序记录")
        rec = self._get(record_id)
        if rec.status is not RecordStatus.OPEN:
            raise RecordError(f"记录 {record_id} 已{rec.status.value}，不得重复签署")
        if rec.ended_at is None:
            raise RecordError(f"记录 {record_id} 尚未收工，不能签署")
        problems = self.specs.validate_record(rec)
        if require_spec_ok and problems:
            raise RecordError(
                f"记录 {record_id} 不合规，不能签署：{'；'.join(problems)}"
            )
        rec = replace(
            rec, status=RecordStatus.CONFIRMED, confirmed_by=actor.user_id,
            signed_at=at,
        )
        self.records[record_id] = rec
        return rec

    def correct_with_deviation(
        self, record_id: str, actor: User, deviation_id: str, at: datetime,
        *, points: list[CurvePoint], parameters: dict[str, Decimal] | None = None,
        digest: str, ended_at: datetime | None = None,
    ) -> ProcessRecord:
        """凭偏差单出具修正记录，替代已确认的原记录，原记录留痕。"""
        if actor.role is not Role.QA:
            raise RecordError(f"角色 {actor.role.value} 不得凭偏差修正记录")
        old = self._get(record_id)
        if old.status is not RecordStatus.CONFIRMED:
            raise RecordError(
                f"只有已确认记录才需要偏差修正，{record_id} 状态为 {old.status.value}"
            )
        corrected = ProcessRecord(
            record_id=self._rid(), lot_id=old.lot_id, kind=old.kind,
            product=old.product,
            specification_version=old.specification_version,
            started_at=old.started_at,
            ended_at=ended_at or old.ended_at,
            parameters={**old.parameters, **(parameters or {})},
            curve=tuple(sorted(points, key=lambda p: p.at)),
            sensor_digest=digest, confirmed_by=actor.user_id,
            status=RecordStatus.CONFIRMED, signed_at=at,
            deviation_id=deviation_id, supersedes=old.record_id,
        )
        problems = self.specs.validate_record(corrected)
        if problems:
            raise RecordError(
                f"修正记录仍不合规：{'；'.join(problems)}"
            )
        self.records[record_id] = replace(old, status=RecordStatus.CORRECTED)
        self.records[corrected.record_id] = corrected
        return corrected

    # ------------------------------------------------------------------ 查询
    def for_lot(self, lot_id: str) -> list[ProcessRecord]:
        return [r for r in self.records.values() if r.lot_id == lot_id]

    def active_for_lot(self, lot_id: str) -> list[ProcessRecord]:
        """每道工序当前生效的记录（排除已被替代的 CORRECTED）。"""
        latest: dict[ProcessKind, ProcessRecord] = {}
        for rec in self.for_lot(lot_id):
            if rec.status is RecordStatus.CORRECTED:
                continue
            latest[rec.kind] = rec
        return [latest[k] for k in sorted(latest, key=lambda x: x.value)]

    def spec_problems(self, lot_id: str) -> dict[str, list[str]]:
        return {
            r.record_id: self.specs.validate_record(r)
            for r in self.active_for_lot(lot_id)
        }
