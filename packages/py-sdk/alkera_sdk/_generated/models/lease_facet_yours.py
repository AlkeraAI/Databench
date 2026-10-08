from enum import StrEnum


class LeaseFacetYours(StrEnum):
    BOX = "box"
    CHAT = "chat"
    NONE = "none"
    YOU = "you"

    def __str__(self) -> str:
        return str(self.value)
