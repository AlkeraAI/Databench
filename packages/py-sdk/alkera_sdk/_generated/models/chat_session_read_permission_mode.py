from enum import StrEnum


class ChatSessionReadPermissionMode(StrEnum):
    AUTO = "auto"
    BYPASS = "bypass"
    DEFAULT = "default"
    PLAN = "plan"
    READ_ONLY = "read_only"

    def __str__(self) -> str:
        return str(self.value)
