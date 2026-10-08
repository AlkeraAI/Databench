from enum import StrEnum


class LostMachineReadReason(StrEnum):
    DELETED = "deleted"
    NO_ACCESS = "no_access"

    def __str__(self) -> str:
        return str(self.value)
