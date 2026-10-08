from enum import StrEnum


class WorkspaceReadMirrorStateType0(StrEnum):
    ASLEEP = "asleep"
    AWAKE = "awake"

    def __str__(self) -> str:
        return str(self.value)
