from enum import StrEnum


class FleetMachineRowSandbox(StrEnum):
    GVISOR = "gvisor"
    NONE = "none"

    def __str__(self) -> str:
        return str(self.value)
