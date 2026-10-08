from enum import StrEnum


class PresenceKindType0(StrEnum):
    AGENT = "agent"
    PERSON = "person"
    SYSTEM = "system"

    def __str__(self) -> str:
        return str(self.value)
