from enum import StrEnum


class ChatModelOptionState(StrEnum):
    AVAILABLE = "available"
    CURRENT = "current"
    UNAVAILABLE = "unavailable"

    def __str__(self) -> str:
        return str(self.value)
