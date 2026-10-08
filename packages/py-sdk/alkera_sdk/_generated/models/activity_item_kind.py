from enum import StrEnum


class ActivityItemKind(StrEnum):
    EDIT = "edit"
    RUN = "run"

    def __str__(self) -> str:
        return str(self.value)
