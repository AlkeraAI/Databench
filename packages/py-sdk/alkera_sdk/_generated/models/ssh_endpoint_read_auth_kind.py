from enum import StrEnum


class SshEndpointReadAuthKind(StrEnum):
    PASSWORD = "password"
    PRIVATE_KEY = "private_key"

    def __str__(self) -> str:
        return str(self.value)
