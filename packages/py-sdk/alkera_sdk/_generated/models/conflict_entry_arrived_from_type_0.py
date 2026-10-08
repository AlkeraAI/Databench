from enum import StrEnum


class ConflictEntryArrivedFromType0(StrEnum):
    HOLDER = "holder"
    WEB = "web"

    def __str__(self) -> str:
        return str(self.value)
