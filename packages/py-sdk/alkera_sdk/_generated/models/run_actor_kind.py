from enum import StrEnum


class RunActorKind(StrEnum):
    AGENT = "agent"
    PERSON = "person"
    SYSTEM = "system"

    def __str__(self) -> str:
        return str(self.value)
