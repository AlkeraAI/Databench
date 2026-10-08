from enum import StrEnum


class BulkItemOp(StrEnum):
    COPY = "copy"
    CREATEFOLDER = "createFolder"
    MOVE = "move"
    STAR = "star"
    TRASH = "trash"
    UNSTAR = "unstar"

    def __str__(self) -> str:
        return str(self.value)
