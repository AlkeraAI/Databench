from enum import StrEnum


class AudienceGrantKind(StrEnum):
    ORG = "org"
    TEAM = "team"
    USER = "user"

    def __str__(self) -> str:
        return str(self.value)
