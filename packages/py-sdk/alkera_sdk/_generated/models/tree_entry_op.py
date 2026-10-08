from enum import StrEnum


class TreeEntryOp(StrEnum):
    DELETE = "delete"
    RENAME = "rename"
    UPSERT = "upsert"

    def __str__(self) -> str:
        return str(self.value)
