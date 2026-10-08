from enum import StrEnum


class MachineRowOrigin(StrEnum):
    PROVISIONED = "provisioned"
    REGISTERED = "registered"

    def __str__(self) -> str:
        return str(self.value)
