from enum import StrEnum


class LiveFacetContent(StrEnum):
    BEHIND = "behind"
    NONE = "none"
    ON_DRIVE = "on_drive"
    UNLANDED = "unlanded"
    UNSYNCED = "unsynced"

    def __str__(self) -> str:
        return str(self.value)
