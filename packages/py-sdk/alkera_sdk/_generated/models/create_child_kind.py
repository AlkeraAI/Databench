from enum import StrEnum


class CreateChildKind(StrEnum):
    FILE = "file"
    FOLDER = "folder"
    SPECIAL = "special"
    SYMLINK = "symlink"

    def __str__(self) -> str:
        return str(self.value)
