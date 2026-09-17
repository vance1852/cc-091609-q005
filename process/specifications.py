"""生产品种工艺规程与生效版本校验。

工艺参数按「品种 + 生效版本」校验：工序记录登记的版本必须在开工时
已经生效且尚未被新版替代；参数与炒制曲线须落在该版本的限度内。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from .contracts import ProcessKind, ProcessRecord
from .errors import SpecificationError

ZERO = Decimal("0")


@dataclass(frozen=True)
class ParamLimit:
    lower: Decimal | None
    upper: Decimal | None
    unit: str
    label: str

    def check(self, value: Decimal) -> str | None:
        if self.lower is not None and value < self.lower:
            return f"{self.label} {value} {self.unit} 低于限度 {self.lower}"
        if self.upper is not None and value > self.upper:
            return f"{self.label} {value} {self.unit} 高于限度 {self.upper}"
        return None


@dataclass(frozen=True)
class CurveRule:
    """炒制温度曲线要求。"""

    temp_lower: Decimal
    temp_upper: Decimal
    max_gap: timedelta          # 相邻采样点最大允许间隔
    lead_tolerance: timedelta   # 开工后首个采样点允许延迟
    tail_tolerance: timedelta   # 收工前最后一个采样点允许提前

    def check(self, record: ProcessRecord) -> list[str]:
        problems: list[str] = []
        if record.ended_at is None:
            return ["工序尚未收工，曲线不完整"]
        curve = sorted(record.curve, key=lambda p: p.at)
        if not curve:
            return ["炒制温度曲线缺失"]

        if curve[0].at - record.started_at > self.lead_tolerance:
            problems.append(
                f"曲线起点延迟 {curve[0].at - record.started_at}，"
                f"超过允许 {self.lead_tolerance}"
            )
        if record.ended_at - curve[-1].at > self.tail_tolerance:
            problems.append(
                f"曲线尾部缺口 {record.ended_at - curve[-1].at}，"
                f"超过允许 {self.tail_tolerance}"
            )

        for prev, point in zip(curve, curve[1:]):
            gap = point.at - prev.at
            if gap > self.max_gap:
                problems.append(
                    f"温度曲线存在 {int(gap.total_seconds() // 60)} 分钟缺口"
                    f"（{prev.at:%H:%M}–{point.at:%H:%M}，"
                    f"允许间隔 {int(self.max_gap.total_seconds())} 秒）"
                )
        for point in curve:
            if not (self.temp_lower <= point.temp_c <= self.temp_upper):
                problems.append(
                    f"{point.at:%H:%M} 温度 {point.temp_c}°C 越限"
                    f"（{self.temp_lower}–{self.temp_upper}°C）"
                )
        return problems


@dataclass(frozen=True)
class StepSpec:
    kind: ProcessKind
    label: str
    params: dict[str, ParamLimit] = field(default_factory=dict)
    curve: CurveRule | None = None


@dataclass(frozen=True)
class ProcessVersion:
    product: str
    version: str
    effective_from: datetime
    steps: dict[ProcessKind, StepSpec]
    note: str = ""


STANDARD_FLOW: tuple[ProcessKind, ...] = (
    ProcessKind.CLEAN,
    ProcessKind.CUT,
    ProcessKind.ADD_EXCIPIENT,
    ProcessKind.MOISTEN,
    ProcessKind.FRY,
    ProcessKind.DRY,
    ProcessKind.SAMPLE,
    ProcessKind.PACK,
)


def _d(value: str) -> Decimal:
    return Decimal(value)


def _default_versions() -> dict[str, list[ProcessVersion]]:
    """酒炙当归工艺规程。v2023 为现行版，v2020 已于 2023-09-01 停止使用。"""
    steps_2023 = {
        ProcessKind.CLEAN: StepSpec(
            ProcessKind.CLEAN, "净制",
            {"foreign_matter_pct": ParamLimit(_d("0"), _d("3.0"), "%", "杂质率")},
        ),
        ProcessKind.CUT: StepSpec(
            ProcessKind.CUT, "切制",
            {"slice_thickness_mm": ParamLimit(_d("1.5"), _d("3.0"), "mm", "切片厚度")},
        ),
        ProcessKind.ADD_EXCIPIENT: StepSpec(
            ProcessKind.ADD_EXCIPIENT, "黄酒加入",
            {"wine_rate_pct": ParamLimit(_d("10.0"), _d("15.0"), "%", "黄酒投料比"),
             "wine_alcohol_pct": ParamLimit(_d("12.0"), _d("20.0"), "%", "黄酒酒精度")},
        ),
        ProcessKind.MOISTEN: StepSpec(
            ProcessKind.MOISTEN, "润制",
            {"moisten_minutes": ParamLimit(_d("60"), _d("180"), "min", "润制时间"),
             "moisten_temp_c": ParamLimit(_d("15"), _d("35"), "°C", "润制温度")},
        ),
        ProcessKind.FRY: StepSpec(
            ProcessKind.FRY, "炒制",
            {"fry_minutes": ParamLimit(_d("8"), _d("20"), "min", "炒制时间")},
            curve=CurveRule(
                temp_lower=_d("140"), temp_upper=_d("180"),
                max_gap=timedelta(seconds=60),
                lead_tolerance=timedelta(seconds=60),
                tail_tolerance=timedelta(seconds=60),
            ),
        ),
        ProcessKind.DRY: StepSpec(
            ProcessKind.DRY, "干燥",
            {"dry_temp_c": ParamLimit(_d("50"), _d("70"), "°C", "干燥温度"),
             "moisture_pct": ParamLimit(_d("0"), _d("10.0"), "%", "干燥后水分")},
        ),
        ProcessKind.SAMPLE: StepSpec(ProcessKind.SAMPLE, "取样"),
        ProcessKind.PACK: StepSpec(ProcessKind.PACK, "包装"),
    }

    steps_2020 = {
        ProcessKind.CLEAN: steps_2023[ProcessKind.CLEAN],
        ProcessKind.CUT: steps_2023[ProcessKind.CUT],
        ProcessKind.ADD_EXCIPIENT: StepSpec(
            ProcessKind.ADD_EXCIPIENT, "黄酒加入",
            {"wine_rate_pct": ParamLimit(_d("8.0"), _d("12.0"), "%", "黄酒投料比")},
        ),
        ProcessKind.MOISTEN: steps_2023[ProcessKind.MOISTEN],
        ProcessKind.FRY: StepSpec(
            ProcessKind.FRY, "炒制",
            {"fry_minutes": ParamLimit(_d("10"), _d("25"), "min", "炒制时间")},
            curve=CurveRule(
                temp_lower=_d("130"), temp_upper=_d("190"),
                max_gap=timedelta(seconds=120),
                lead_tolerance=timedelta(seconds=120),
                tail_tolerance=timedelta(seconds=120),
            ),
        ),
        ProcessKind.DRY: steps_2023[ProcessKind.DRY],
        ProcessKind.SAMPLE: StepSpec(ProcessKind.SAMPLE, "取样"),
        ProcessKind.PACK: StepSpec(ProcessKind.PACK, "包装"),
    }

    return {
        "酒炙当归": [
            ProcessVersion("酒炙当归", "v2020", datetime(2020, 9, 1), steps_2020,
                           note="旧版，2023-09-01 起停用"),
            ProcessVersion("酒炙当归", "v2023", datetime(2023, 9, 1), steps_2023,
                           note="现行版"),
        ],
    }


class SpecificationRegistry:
    def __init__(self, versions: dict[str, list[ProcessVersion]] | None = None):
        table: dict[tuple[str, str], ProcessVersion] = {}
        for product, plist in (versions or _default_versions()).items():
            for pv in plist:
                table[(product, pv.version)] = pv
        self._versions = table

    def effective_version(self, product: str, at: datetime) -> ProcessVersion:
        current = None
        for pv in self._versions.values():
            if pv.product != product:
                continue
            if pv.effective_from <= at and (
                current is None or pv.effective_from > current.effective_from
            ):
                current = pv
        if current is None:
            raise SpecificationError(f"品种 {product} 在 {at:%Y-%m-%d} 无生效工艺版本")
        return current

    def get(self, product: str, version: str) -> ProcessVersion:
        try:
            return self._versions[(product, version)]
        except KeyError:
            raise SpecificationError(
                f"品种 {product} 不存在工艺版本 {version}"
            ) from None

    def validate_record(
        self, record: ProcessRecord, *, require_curve: bool = True
    ) -> list[str]:
        """返回该记录的全部违规项；空列表表示合规。"""
        pv = self.get(record.product, record.specification_version)
        effective = self.effective_version(record.product, record.started_at)
        problems: list[str] = []
        if effective.version != record.specification_version:
            problems.append(
                f"{record.kind.value} 记录使用 {record.specification_version}，"
                f"开工日 {record.started_at:%Y-%m-%d} 现行版本为 {effective.version}"
            )
        step = pv.steps.get(record.kind)
        if step is None:
            problems.append(f"版本 {pv.version} 未规定工步 {record.kind.value}")
            return problems

        for name, limit in step.params.items():
            if name not in record.parameters:
                problems.append(f"{step.label} 缺少参数 {limit.label}")
                continue
            problem = limit.check(record.parameters[name])
            if problem:
                problems.append(problem)
        extra = set(record.parameters) - set(step.params)
        if extra:
            problems.append(f"{step.label} 存在未登记参数：{', '.join(sorted(extra))}")

        if require_curve and step.curve is not None and record.ended_at is not None:
            problems.extend(step.curve.check(record))
        return problems
