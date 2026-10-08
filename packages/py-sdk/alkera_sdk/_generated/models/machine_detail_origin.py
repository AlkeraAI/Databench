from enum import StrEnum


class MachineDetailOrigin(StrEnum):
    PROVISIONED = "provisioned"
    REGISTERED = "registered"

    def __str__(self) -> str:
        return str(self.value)
