"""领域错误。"""


class ProcessError(Exception):
    """所有可预期业务规则违例的基类。"""


class ConservationError(ProcessError):
    """拆分/合批/损耗不满足数量守恒。"""


class LedgerError(ProcessError):
    """物料台账错误：批号不存在、库存不足、状态不允许等。"""


class SpecificationError(ProcessError):
    """工艺版本不存在或工艺参数/曲线越限。"""


class RecordError(ProcessError):
    """工序记录状态不允许该操作（如已确认记录被直接补传）。"""


class WorkflowError(ProcessError):
    """偏差/复检/返工/放行流转或权限错误。"""


class ReleaseError(ProcessError):
    """放行条件不满足（资料缺口、报告借用、范围不符等）。"""
