from enum import StrEnum


class MachineReleasedTenancy(StrEnum):
    DEDICATED = "dedicated"
    ORG = "org"
    PERSONAL = "personal"
    POOL = "pool"

    def __str__(self) -> str:
        return str(self.value)
