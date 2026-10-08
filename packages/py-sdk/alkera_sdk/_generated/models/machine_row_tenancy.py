from enum import StrEnum


class MachineRowTenancy(StrEnum):
    DEDICATED = "dedicated"
    ORG = "org"
    PERSONAL = "personal"
    POOL = "pool"

    def __str__(self) -> str:
        return str(self.value)
