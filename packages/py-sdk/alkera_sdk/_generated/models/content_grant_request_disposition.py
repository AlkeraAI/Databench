from enum import StrEnum


class ContentGrantRequestDisposition(StrEnum):
    ATTACHMENT = "attachment"
    INLINE = "inline"

    def __str__(self) -> str:
        return str(self.value)
