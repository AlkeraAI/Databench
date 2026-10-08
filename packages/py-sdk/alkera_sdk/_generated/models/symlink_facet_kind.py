from enum import StrEnum


class SymlinkFacetKind(StrEnum):
    CANONICAL = "canonical"
    HOST = "host"
    RELATIVE = "relative"

    def __str__(self) -> str:
        return str(self.value)
