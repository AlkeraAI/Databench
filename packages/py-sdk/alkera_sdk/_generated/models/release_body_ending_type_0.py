from enum import StrEnum


class ReleaseBodyEndingType0(StrEnum):
    DRAINED = "drained"
    EVICTED = "evicted"
    IDLE = "idle"

    def __str__(self) -> str:
        return str(self.value)
