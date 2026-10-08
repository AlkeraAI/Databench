from enum import StrEnum


class PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior(StrEnum):
    FAIL = "fail"
    RENAME = "rename"

    def __str__(self) -> str:
        return str(self.value)
