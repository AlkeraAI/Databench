from enum import StrEnum


class ResolveRequestKeep(StrEnum):
    BOTH = "both"
    MINE = "mine"
    THEIRS = "theirs"

    def __str__(self) -> str:
        return str(self.value)
