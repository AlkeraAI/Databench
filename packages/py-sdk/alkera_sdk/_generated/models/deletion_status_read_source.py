from enum import StrEnum


class DeletionStatusReadSource(StrEnum):
    RESTORE = "restore"
    SELF = "self"
    SUPPORT = "support"

    def __str__(self) -> str:
        return str(self.value)
