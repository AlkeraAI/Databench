from enum import StrEnum


class ChatTemplateReadPermissionMode(StrEnum):
    AUTO = "auto"
    BYPASS = "bypass"
    DEFAULT = "default"
    PLAN = "plan"
    READ_ONLY = "read_only"

    def __str__(self) -> str:
        return str(self.value)
