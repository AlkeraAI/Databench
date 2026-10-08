from enum import StrEnum


class TokenType(StrEnum):
    CLI = "cli"
    SESSION = "session"

    def __str__(self) -> str:
        return str(self.value)
