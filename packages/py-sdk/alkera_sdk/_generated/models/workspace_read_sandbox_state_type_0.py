from enum import StrEnum


class WorkspaceReadSandboxStateType0(StrEnum):
    ASLEEP = "asleep"
    AWAKE = "awake"
    WAKING = "waking"

    def __str__(self) -> str:
        return str(self.value)
