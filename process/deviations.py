"""偏差单生命周期与权限分离。

状态机::

    OPEN ──QA 评估──▶ ASSESSED ──QA 验证处置──▶ VERIFIED ──QA 关闭──▶ CLOSED
                          ▲                         │
                          └─── 复检后改判可再评估 ───┘

权限：任何人可发起偏差；只有 QA 能评估、验证、关闭。
具体处置（复检取样/检验、返工操作、账目核减、记录修正）由 QC、操作人
等其他角色执行，QA 只负责核实，形成角色制衡。
"""

from dataclasses import replace
from datetime import datetime

from .contracts import Deviation, DeviationStatus, DispositionKind, Role, User
from .errors import WorkflowError


class DeviationManager:
    def __init__(self) -> None:
        self.deviations: dict[str, Deviation] = {}
        self._seq = 0

    def _did(self) -> str:
        self._seq += 1
        return f"dev-{self._seq:03d}"

    def get(self, deviation_id: str) -> Deviation:
        try:
            return self.deviations[deviation_id]
        except KeyError:
            raise WorkflowError(f"偏差单 {deviation_id} 不存在") from None

    def raise_deviation(
        self, actor: User, *, lot_id: str, kind: str, title: str,
        at: datetime, detail: str = "",
        impacted_records: tuple[str, ...] = (),
    ) -> Deviation:
        dev = Deviation(
            deviation_id=self._did(), lot_id=lot_id, kind=kind, title=title,
            detail=detail, impacted_records=tuple(impacted_records),
            raised_by=actor.user_id, raised_at=at,
            status=DeviationStatus.OPEN,
        )
        self.deviations[dev.deviation_id] = dev
        return dev

    def assess(
        self, actor: User, deviation_id: str, disposition: DispositionKind,
        at: datetime, *, root_cause: str, evidence: tuple[str, ...] = (),
    ) -> Deviation:
        if actor.role is not Role.QA:
            raise WorkflowError(f"角色 {actor.role.value} 不得评估偏差，须由 QA 评估")
        dev = self.get(deviation_id)
        if dev.status not in (
            DeviationStatus.OPEN,
            DeviationStatus.ASSESSED,
            DeviationStatus.VERIFIED,
            DeviationStatus.CLOSED,
        ):
            raise WorkflowError(
                f"偏差 {deviation_id} 状态为 {dev.status.value}，不能评估/改判"
            )
        dev = replace(
            dev, status=DeviationStatus.ASSESSED, disposition=disposition,
            root_cause=root_cause, assessed_by=actor.user_id, assessed_at=at,
            evidence=tuple(set(dev.evidence) | set(evidence)),
        )
        self.deviations[deviation_id] = dev
        return dev

    def verify(
        self, actor: User, deviation_id: str, at: datetime,
        *, evidence: tuple[str, ...] = (),
    ) -> Deviation:
        if actor.role is not Role.QA:
            raise WorkflowError(f"角色 {actor.role.value} 不得验证处置，须由 QA 验证")
        dev = self.get(deviation_id)
        if dev.status is not DeviationStatus.ASSESSED:
            raise WorkflowError(
                f"偏差 {deviation_id} 尚未评估或已关闭，不能验证"
            )
        if not dev.disposition:
            raise WorkflowError(f"偏差 {deviation_id} 无处置结论")
        dev = replace(
            dev, status=DeviationStatus.VERIFIED,
            verified_by=actor.user_id, verified_at=at,
            evidence=tuple(set(dev.evidence) | set(evidence)),
        )
        self.deviations[deviation_id] = dev
        return dev

    def close(self, actor: User, deviation_id: str, at: datetime) -> Deviation:
        if actor.role is not Role.QA:
            raise WorkflowError(f"角色 {actor.role.value} 不得关闭偏差，须由 QA 关闭")
        dev = self.get(deviation_id)
        if dev.status is not DeviationStatus.VERIFIED:
            raise WorkflowError(
                f"偏差 {deviation_id} 处置未经 QA 验证，不能关闭"
            )
        dev = replace(
            dev, status=DeviationStatus.CLOSED,
            closed_by=actor.user_id, closed_at=at,
        )
        self.deviations[deviation_id] = dev
        return dev

    def for_lot(self, lot_id: str) -> list[Deviation]:
        return [d for d in self.deviations.values() if d.lot_id == lot_id]
