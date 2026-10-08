from enum import StrEnum


class WorkspaceReadLayout(StrEnum):
    ADOPTED = "adopted"
    NATIVE = "native"

    def __str__(self) -> str:
        return str(self.value)
