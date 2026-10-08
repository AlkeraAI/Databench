from enum import StrEnum


class CellStateOutputOriginType0(StrEnum):
    KERNEL = "kernel"
    SAVED = "saved"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
