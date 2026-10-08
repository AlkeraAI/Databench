from enum import StrEnum


class BulkItemConflictbehavior(StrEnum):
    FAIL = "fail"
    RENAME = "rename"

    def __str__(self) -> str:
        return str(self.value)
