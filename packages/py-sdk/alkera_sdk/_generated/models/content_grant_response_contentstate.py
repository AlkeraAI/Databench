from enum import StrEnum


class ContentGrantResponseContentstate(StrEnum):
    BEHIND = "behind"
    NONE = "none"
    ON_DRIVE = "on_drive"
    UNLANDED = "unlanded"
    UNSYNCED = "unsynced"

    def __str__(self) -> str:
        return str(self.value)
