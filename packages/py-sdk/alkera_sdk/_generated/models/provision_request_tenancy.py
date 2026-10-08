from enum import StrEnum


class ProvisionRequestTenancy(StrEnum):
    DEDICATED = "dedicated"
    POOL = "pool"

    def __str__(self) -> str:
        return str(self.value)
