"""取样与检验报告（QC）。

报告只对其签发批次和所列样品负责：样品必须取自被检批，返工后的新批
必须重新补采并出具新报告，系统在放行环节拒绝跨批借用旧报告。
"""

from datetime import datetime
from decimal import Decimal

from .contracts import (
    InspectionReport,
    Role,
    Sample,
    SampleKind,
    TestResult,
    User,
)
from .errors import WorkflowError
from .ledger import MaterialLedger


class QualityLab:
    def __init__(self, ledger: MaterialLedger) -> None:
        self.ledger = ledger
        self.samples: dict[str, Sample] = {}
        self.reports: dict[str, InspectionReport] = {}
        self._sseq = 0
        self._rseq = 0

    def _sid(self) -> str:
        self._sseq += 1
        return f"smp-{self._sseq:03d}"

    def _rid(self) -> str:
        self._rseq += 1
        return f"rpt-{self._rseq:03d}"

    def take_sample(
        self, actor: User, lot_id: str, quantity_kg: Decimal, at: datetime,
        *, kind: SampleKind = SampleKind.ROUTINE, note: str = "",
    ) -> Sample:
        if actor.role is not Role.QC:
            raise WorkflowError(f"角色 {actor.role.value} 不得取样，须由 QC 取样")
        sample_id = self._sid()
        self.ledger.take_sample(lot_id, sample_id, quantity_kg, at, note=note)
        sample = Sample(
            sample_id=sample_id, lot_id=lot_id, quantity_kg=quantity_kg,
            taken_at=at, taken_by=actor.user_id, kind=kind, note=note,
        )
        self.samples[sample_id] = sample
        return sample

    def issue_report(
        self, actor: User, lot_id: str, sample_ids: tuple[str, ...] | list[str],
        results: tuple[TestResult, ...] | list[TestResult], at: datetime,
        *, specification_version: str,
    ) -> InspectionReport:
        if actor.role is not Role.QC:
            raise WorkflowError(f"角色 {actor.role.value} 不得签发检验报告")
        sample_ids = tuple(sample_ids)
        if not sample_ids:
            raise WorkflowError("检验报告必须引用至少一个样品")
        for sid in sample_ids:
            sample = self.samples.get(sid)
            if sample is None:
                raise WorkflowError(f"样品 {sid} 不存在")
            if sample.lot_id != lot_id:
                raise WorkflowError(
                    f"样品 {sid} 取自 {sample.lot_id}，"
                    f"不得用于 {lot_id} 的报告（禁止跨批借用样品）"
                )
        report = InspectionReport(
            report_id=self._rid(), lot_id=lot_id, sample_ids=sample_ids,
            issued_by=actor.user_id, issued_at=at, results=tuple(results),
            specification_version=specification_version,
        )
        self.reports[report.report_id] = report
        return report

    def samples_of(self, lot_id: str) -> list[Sample]:
        return [s for s in self.samples.values() if s.lot_id == lot_id]

    def report_for(self, lot_id: str) -> InspectionReport | None:
        reports = [r for r in self.reports.values() if r.lot_id == lot_id]
        return max(reports, key=lambda r: r.issued_at) if reports else None
