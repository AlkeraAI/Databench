from enum import StrEnum


class ChatPublisherStateUpdateEnding(StrEnum):
    DRAINED = "drained"
    EVICTED = "evicted"
    IDLE = "idle"

    def __str__(self) -> str:
        return str(self.value)
