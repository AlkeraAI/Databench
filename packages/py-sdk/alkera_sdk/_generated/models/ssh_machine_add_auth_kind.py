from enum import StrEnum


class SshMachineAddAuthKind(StrEnum):
    PASSWORD = "password"
    PRIVATE_KEY = "private_key"

    def __str__(self) -> str:
        return str(self.value)
