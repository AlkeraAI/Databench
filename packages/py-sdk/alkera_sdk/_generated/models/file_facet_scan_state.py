from enum import StrEnum


class FileFacetScanState(StrEnum):
    CLEAN = "clean"
    INFECTED = "infected"
    PENDING = "pending"
    SKIPPED = "skipped"

    def __str__(self) -> str:
        return str(self.value)
