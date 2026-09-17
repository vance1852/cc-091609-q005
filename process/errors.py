"""领域异常。"""


class PlatformError(Exception):
    """平台业务规则被违反。"""


class AuthorizationError(PlatformError):
    """当前角色无权执行该操作。"""


class ConservationError(PlatformError):
    """物料移动不满足数量守恒：投入 = 产出 + 损耗。"""


class SpecificationError(PlatformError):
    """工艺参数或曲线不符合该品种当前生效版本。"""


class RecordStateError(PlatformError):
    """工序记录状态不允许该操作（如已签署记录被直接改写）。"""


class DeviationStateError(PlatformError):
    """偏差单状态不允许该流转。"""


class ReportBorrowingError(PlatformError):
    """检验报告与样品/批号不一致，属于借用或冒用报告。"""


class ReleaseBlocked(PlatformError):
    """放行证据链不完整。"""

    def __init__(self, failures: list[str]):
        self.failures = failures
        super().__init__("；".join(failures))
