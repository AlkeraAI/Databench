from enum import StrEnum


class EventActorKind(StrEnum):
    ADMIN = "admin"
    BOX = "box"
    MEMBER = "member"
    SYSTEM = "system"

    def __str__(self) -> str:
        return str(self.value)
