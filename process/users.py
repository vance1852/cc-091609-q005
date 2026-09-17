"""用户与角色权限。

四类关键流转必须由不同角色推进：
偏差评估（deviation_owner/QA）、复检（QA 取样、QC 不在本模型中单列，由 QA 代理）、
返工批准（rework_planner）、放行（qp）。操作人只能执行并签署本人工序记录。
"""

from dataclasses import dataclass

from .contracts import Role
from .errors import AuthorizationError


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    roles: tuple[Role, ...]

    def has(self, role: Role) -> bool:
        return role in self.roles

    def require(self, role: Role) -> None:
        if not self.has(role):
            raise AuthorizationError(
                f"用户 {self.user_id}（{self.name}）缺少角色 {role.value}，"
                f"不得执行该操作"
            )
