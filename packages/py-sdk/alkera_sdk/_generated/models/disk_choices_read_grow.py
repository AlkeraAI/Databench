from enum import StrEnum


class DiskChoicesReadGrow(StrEnum):
    NEVER = "never"
    ONLINE = "online"
    RESTART = "restart"

    def __str__(self) -> str:
        return str(self.value)
