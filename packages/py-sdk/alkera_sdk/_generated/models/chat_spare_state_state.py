from enum import StrEnum


class ChatSpareStateState(StrEnum):
    NONE = "none"
    WARM = "warm"

    def __str__(self) -> str:
        return str(self.value)
