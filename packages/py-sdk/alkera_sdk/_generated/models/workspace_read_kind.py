from enum import StrEnum


class WorkspaceReadKind(StrEnum):
    MAIN = "main"
    PROJECT = "project"

    def __str__(self) -> str:
        return str(self.value)
