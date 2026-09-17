"""按生产品种与生效版本管理工艺规格。

一份 :class:`ProcessSpecification` 覆盖该品种从净制到包装的全部关键工序；
参数校验严格按 *品种 + 版本* 进行，曲线规则（炒制温度窗口、采样间隔、
未挂偏差时允许的最大缺口）同样随版本走。
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .contracts import ProcessKind
from .errors import SpecificationError


@dataclass(frozen=True)
class ParamRule:
    """单个参数的版本化规则。"""

    name: str
    low: Decimal
    high: Decimal
    unit: str
    required: bool = True
    integer: bool = False

    def validate(self, product: str, version: str, kind: ProcessKind, raw: str) -> Decimal:
        try:
            value = Decimal(raw)
        except Exception as exc:  # noqa: BLE001 - 统一转领域异常
            raise SpecificationError(
                f"[{product}@{version}] 工序 {kind.value} 参数 {self.name} "
                f"值 {raw!r} 无法解析为数值"
            ) from exc
        if self.integer and value != value.to_integral_value():
            raise SpecificationError(
                f"[{product}@{version}] 工序 {kind.value} 参数 {self.name} "
                f"必须为整数，实测 {raw}"
            )
        if not (self.low <= value <= self.high):
            raise SpecificationError(
                f"[{product}@{version}] 工序 {kind.value} 参数 {self.name} 超出 "
                f"{self.low}{self.unit}~{self.high}{self.unit}，实测 {value}{self.unit}"
            )
        return value


@dataclass(frozen=True)
class CurveRule:
    """炒制温度曲线的版本化要求。"""

    step_seconds: int = 30                 # 标称采样间隔
    tolerance_seconds: int = 5             # 间隔容差
    temp_low: Decimal = Decimal("120")     # 文火温度下限
    temp_high: Decimal = Decimal("180")    # 温度上限
    max_unsigned_gap_seconds: int = 0      # 未挂偏差时允许的最大缺口；0=不允许

    def check_interval(self, delta: int) -> int | None:
        """返回超过标称间隔的缺口秒数；正常间隔返回 None。"""
        if delta <= self.step_seconds + self.tolerance_seconds:
            return None
        return delta - self.step_seconds


def _d(text: str) -> Decimal:
    return Decimal(text)


@dataclass(frozen=True)
class ProcessSpecification:
    product: str
    version: str
    effective_from: datetime
    required_steps: tuple[ProcessKind, ...]
    param_rules: dict[ProcessKind, dict[str, ParamRule]]
    curve_rule: CurveRule | None = None

    def step_index(self, kind: ProcessKind) -> int:
        return self.required_steps.index(kind)

    def validate_parameters(self, kind: ProcessKind, parameters: dict[str, str]) -> None:
        rules = self.param_rules.get(kind, {})
        for name, rule in rules.items():
            if name not in parameters:
                if rule.required:
                    raise SpecificationError(
                        f"[{self.product}@{self.version}] 工序 {kind.value} "
                        f"缺少必填参数 {name}"
                    )
                continue
            rule.validate(self.product, self.version, kind, parameters[name])

    def validate_curve(self, curve: tuple) -> list[int]:
        """校验曲线温度窗口，返回采样缺口列表（每个缺口超出的秒数）。

        调用方应仅对炒制工序（``curve_rule`` 非空）调用本方法。
        """
        rule = self.curve_rule
        if rule is None:
            return []
        points = sorted(curve, key=lambda p: p.t_seconds)
        gaps: list[int] = []
        for point in points:
            if not (rule.temp_low <= point.temp_c <= rule.temp_high):
                raise SpecificationError(
                    f"[{self.product}@{self.version}] 炒制曲线 {point.t_seconds}s 处温度 "
                    f"{point.temp_c}℃ 超出 {rule.temp_low}~{rule.temp_high}℃"
                )
        for prev, nxt in zip(points, points[1:]):
            gap = rule.check_interval(nxt.t_seconds - prev.t_seconds)
            if gap is not None:
                gaps.append(gap)
        return gaps


def jiuzhi_danggui_spec(version: str = "v2024.1") -> ProcessSpecification:
    """酒炙当归工艺：净制→切制→加酒→润→炒→干燥→取样→包装。

    关键控制参数（示例性工艺规程）：
    * add_excipient: 黄酒比例 10%~15%（相对净药材），酒精度 10~16 度
    * moisten:       润制 30~60 分钟，翻拌 ≥2 次
    * fry:           文火 120~180℃ 炒 8~15 分钟，曲线不允许无偏差缺口
    * dry:           60~80℃ 干燥至水分 8.0%~12.0%
    """

    steps = (
        ProcessKind.CLEAN,
        ProcessKind.CUT,
        ProcessKind.ADD_EXCIPIENT,
        ProcessKind.MOISTEN,
        ProcessKind.FRY,
        ProcessKind.DRY,
        ProcessKind.SAMPLE,
        ProcessKind.PACK,
    )
    rules: dict[ProcessKind, dict[str, ParamRule]] = {
        ProcessKind.CLEAN: {
            "remove_impurity_pct": ParamRule(
                "remove_impurity_pct", _d("0"), _d("5"), "%"
            ),
        },
        ProcessKind.CUT: {
            "slice_thickness_mm": ParamRule(
                "slice_thickness_mm", _d("1"), _d("2"), "mm"
            ),
        },
        ProcessKind.ADD_EXCIPIENT: {
            "wine_ratio_pct": _rule("wine_ratio_pct", "10", "15", "%"),
            "wine_alcohol_degree": _rule("wine_alcohol_degree", "10", "16", "度"),
        },
        ProcessKind.MOISTEN: {
            "moisten_minutes": _rule("moisten_minutes", "30", "60", "min"),
            "turning_count": _rule("turning_count", "2", "10", "次", integer=True),
        },
        ProcessKind.FRY: {
            "set_temp_c": _rule("set_temp_c", "120", "180", "℃"),
            "fry_minutes": _rule("fry_minutes", "8", "15", "min"),
        },
        ProcessKind.DRY: {
            "dry_temp_c": _rule("dry_temp_c", "60", "80", "℃"),
            "moisture_pct": _rule("moisture_pct", "8.0", "12.0", "%"),
        },
        ProcessKind.PACK: {
            "net_weight_g": _rule("net_weight_g", "495", "505", "g"),
        },
    }
    return ProcessSpecification(
        product="酒炙当归",
        version=version,
        effective_from=datetime(2024, 1, 1),
        required_steps=steps,
        param_rules=rules,
        curve_rule=CurveRule(
            step_seconds=30,
            tolerance_seconds=5,
            temp_low=_d("120"),
            temp_high=_d("180"),
            max_unsigned_gap_seconds=0,
        ),
    )


def _rule(name: str, low: str, high: str, unit: str, *, integer: bool = False) -> ParamRule:
    return ParamRule(name, _d(low), _d(high), unit, integer=integer)


class SpecificationRegistry:
    """品种 -> 版本 -> 规格；提供生效版本解析与有效性校验。"""

    def __init__(self) -> None:
        self._specs: dict[str, dict[str, ProcessSpecification]] = {}
        self._retired: set[tuple[str, str]] = set()

    def register(self, spec: ProcessSpecification) -> None:
        self._specs.setdefault(spec.product, {})[spec.version] = spec

    def retire(self, product: str, version: str) -> None:
        self._retired.add((product, version))

    def effective_version(self, product: str, at: datetime) -> str:
        candidates = [
            s
            for s in self._specs.get(product, {}).values()
            if s.effective_from <= at and (product, s.version) not in self._retired
        ]
        if not candidates:
            raise SpecificationError(f"品种 {product} 在 {at} 没有生效中的工艺规格")
        return max(candidates, key=lambda s: s.effective_from).version

    def get(self, product: str, version: str) -> ProcessSpecification:
        try:
            return self._specs[product][version]
        except KeyError:
            raise SpecificationError(
                f"品种 {product} 不存在规格版本 {version}"
            ) from None

    def require_effective(
        self, product: str, version: str, at: datetime
    ) -> ProcessSpecification:
        """批记录只能引用记录发生时点 *正在生效* 的版本。"""
        spec = self.get(product, version)
        if (product, version) in self._retired:
            raise SpecificationError(
                f"品种 {product} 的规格 {version} 已废止，不得用于批记录"
            )
        if spec.effective_from > at:
            raise SpecificationError(
                f"品种 {product} 的规格 {version} 于 {spec.effective_from} 才生效，"
                f"批记录时间 {at} 时尚未生效"
            )
        latest = self.effective_version(product, at)
        if latest != version:
            raise SpecificationError(
                f"品种 {product} 批记录引用版本 {version}，但 {at} 时生效版本为 {latest}"
            )
        return spec
