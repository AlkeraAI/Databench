from enum import StrEnum


class FleetMachineRowOrigin(StrEnum):
    PROVISIONED = "provisioned"
    REGISTERED = "registered"

    def __str__(self) -> str:
        return str(self.value)
