from enum import StrEnum


class CreateChildConflictbehavior(StrEnum):
    FAIL = "fail"
    RENAME = "rename"

    def __str__(self) -> str:
        return str(self.value)
