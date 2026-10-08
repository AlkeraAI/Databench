from enum import StrEnum


class PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior(StrEnum):
    FAIL = "fail"
    RENAME = "rename"
    REPLACE = "replace"

    def __str__(self) -> str:
        return str(self.value)
