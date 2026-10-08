from enum import StrEnum


class CompleteUploadRequestConflictbehavior(StrEnum):
    FAIL = "fail"
    RENAME = "rename"
    REPLACE = "replace"

    def __str__(self) -> str:
        return str(self.value)
