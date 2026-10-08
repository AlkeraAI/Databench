from enum import StrEnum


class ContentGrantResponseKind(StrEnum):
    FILE = "file"
    PAGE = "page"

    def __str__(self) -> str:
        return str(self.value)
