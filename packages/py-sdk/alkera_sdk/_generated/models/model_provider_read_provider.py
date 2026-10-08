from enum import StrEnum


class ModelProviderReadProvider(StrEnum):
    ANTHROPIC = "anthropic"
    BEDROCK = "bedrock"
    OPENAI = "openai"

    def __str__(self) -> str:
        return str(self.value)
