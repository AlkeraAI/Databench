from enum import StrEnum


class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    BEDROCK = "bedrock"
    OPENAI = "openai"

    def __str__(self) -> str:
        return str(self.value)
