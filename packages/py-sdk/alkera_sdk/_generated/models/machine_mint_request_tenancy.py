from enum import StrEnum


class MachineMintRequestTenancy(StrEnum):
    DEDICATED = "dedicated"
    POOL = "pool"

    def __str__(self) -> str:
        return str(self.value)
