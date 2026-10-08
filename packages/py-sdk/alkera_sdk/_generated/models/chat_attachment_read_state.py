from enum import StrEnum


class ChatAttachmentReadState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"

    def __str__(self) -> str:
        return str(self.value)
