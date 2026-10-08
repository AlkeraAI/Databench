from enum import StrEnum


class ModelProviderTestResultStatusType0(StrEnum):
    INVALID_KEY = "invalid_key"
    NETWORK = "network"
    OK = "ok"
    PERMISSION = "permission"

    def __str__(self) -> str:
        return str(self.value)
