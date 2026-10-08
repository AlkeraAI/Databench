from enum import StrEnum


class ModelProviderUpdateRequestBedrockAuthModeType0(StrEnum):
    ACCESS_KEY = "access_key"
    IAM = "iam"

    def __str__(self) -> str:
        return str(self.value)
