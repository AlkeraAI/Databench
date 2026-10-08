from enum import StrEnum


class NotebookConnectionKind(StrEnum):
    PERSONAL = "personal"
    PER_USER = "per_user"
    TEAM = "team"

    def __str__(self) -> str:
        return str(self.value)
