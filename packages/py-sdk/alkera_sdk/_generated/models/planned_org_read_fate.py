from enum import StrEnum


class PlannedOrgReadFate(StrEnum):
    BLOCKED = "blocked"
    CLOSE = "close"
    LEAVE = "leave"

    def __str__(self) -> str:
        return str(self.value)
