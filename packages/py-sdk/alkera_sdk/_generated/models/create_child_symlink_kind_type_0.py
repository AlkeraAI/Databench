from enum import StrEnum


class CreateChildSymlinkKindType0(StrEnum):
    CANONICAL = "canonical"
    HOST = "host"
    RELATIVE = "relative"

    def __str__(self) -> str:
        return str(self.value)
