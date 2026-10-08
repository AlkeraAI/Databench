from enum import StrEnum


class ActorRefKind(StrEnum):
    AGENT = "agent"
    PERSON = "person"
    SYSTEM = "system"

    def __str__(self) -> str:
        return str(self.value)
