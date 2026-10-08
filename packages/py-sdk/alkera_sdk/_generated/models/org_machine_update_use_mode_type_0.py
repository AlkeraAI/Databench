from enum import StrEnum


class OrgMachineUpdateUseModeType0(StrEnum):
    ASSIGNED = "assigned"
    POOL = "pool"

    def __str__(self) -> str:
        return str(self.value)
