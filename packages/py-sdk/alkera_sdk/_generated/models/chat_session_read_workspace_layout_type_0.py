from enum import StrEnum


class ChatSessionReadWorkspaceLayoutType0(StrEnum):
    ADOPTED = "adopted"
    NATIVE = "native"

    def __str__(self) -> str:
        return str(self.value)
