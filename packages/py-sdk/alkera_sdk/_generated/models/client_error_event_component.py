from enum import StrEnum


class ClientErrorEventComponent(StrEnum):
    EXTENSION = "extension"
    WEB = "web"

    def __str__(self) -> str:
        return str(self.value)
