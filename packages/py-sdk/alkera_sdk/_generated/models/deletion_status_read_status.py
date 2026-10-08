from enum import StrEnum


class DeletionStatusReadStatus(StrEnum):
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    SCHEDULED = "scheduled"

    def __str__(self) -> str:
        return str(self.value)
