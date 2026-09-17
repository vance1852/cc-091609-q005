"""炮制批次与质量放行平台（聚合根）。

把物料台账、连续批记录、检验、偏差和放行评审装配为一个平台，
并维护系统用户与其角色。
"""

from .contracts import User
from .deviations import DeviationManager
from .ledger import MaterialLedger
from .quality import QualityLab
from .records import RecordBook
from .release import ReleaseReviewer
from .specifications import SpecificationRegistry


class ProcessingPlatform:
    def __init__(self, specs: SpecificationRegistry | None = None) -> None:
        self.specs = specs or SpecificationRegistry()
        self.ledger = MaterialLedger()
        self.records = RecordBook(self.specs)
        self.deviations = DeviationManager()
        self.lab = QualityLab(self.ledger)
        self.reviewer = ReleaseReviewer(
            self.ledger, self.records, self.lab, self.deviations, self.specs
        )
        self.users: dict[str, User] = {}

    def register_user(self, user: User) -> User:
        self.users[user.user_id] = user
        return user

    def user(self, user_id: str) -> User:
        return self.users[user_id]
