from enum import StrEnum


class WorkspaceDeletionState(StrEnum):
    DELETED = "deleted"
    DELETING = "deleting"

    def __str__(self) -> str:
        return str(self.value)
