from enum import StrEnum


class ChatModelReadWire(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"

    def __str__(self) -> str:
        return str(self.value)
