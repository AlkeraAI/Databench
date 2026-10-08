from enum import StrEnum


class OrgMachineDetailUseMode(StrEnum):
    ASSIGNED = "assigned"
    POOL = "pool"

    def __str__(self) -> str:
        return str(self.value)
