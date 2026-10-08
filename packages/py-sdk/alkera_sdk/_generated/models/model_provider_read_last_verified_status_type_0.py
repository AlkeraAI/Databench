from enum import StrEnum


class ModelProviderReadLastVerifiedStatusType0(StrEnum):
    INVALID_KEY = "invalid_key"
    NETWORK = "network"
    OK = "ok"
    PERMISSION = "permission"

    def __str__(self) -> str:
        return str(self.value)
