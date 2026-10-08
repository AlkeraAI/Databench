from enum import StrEnum


class ContentGrantRequestKind(StrEnum):
    FILE = "file"
    PAGE = "page"

    def __str__(self) -> str:
        return str(self.value)
