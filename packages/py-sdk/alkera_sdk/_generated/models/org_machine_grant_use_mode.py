from enum import StrEnum


class OrgMachineGrantUseMode(StrEnum):
    ASSIGNED = "assigned"
    POOL = "pool"

    def __str__(self) -> str:
        return str(self.value)
