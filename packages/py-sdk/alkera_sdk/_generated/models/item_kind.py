from enum import StrEnum


class ItemKind(StrEnum):
    FILE = "file"
    FOLDER = "folder"
    OBJECT = "object"
    SPECIAL = "special"
    SYMLINK = "symlink"

    def __str__(self) -> str:
        return str(self.value)
