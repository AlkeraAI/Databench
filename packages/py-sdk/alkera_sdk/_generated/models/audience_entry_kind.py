from enum import StrEnum


class AudienceEntryKind(StrEnum):
    ORG = "org"
    TEAM = "team"
    USER = "user"

    def __str__(self) -> str:
        return str(self.value)
