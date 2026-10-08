from enum import StrEnum


class MachineReleasedOrigin(StrEnum):
    PROVISIONED = "provisioned"
    REGISTERED = "registered"

    def __str__(self) -> str:
        return str(self.value)
