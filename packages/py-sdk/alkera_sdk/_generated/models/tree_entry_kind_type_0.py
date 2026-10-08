from enum import StrEnum


class TreeEntryKindType0(StrEnum):
    DIR = "dir"
    FILE = "file"

    def __str__(self) -> str:
        return str(self.value)
