from enum import StrEnum


class MachineDetailSandbox(StrEnum):
    GVISOR = "gvisor"
    NONE = "none"

    def __str__(self) -> str:
        return str(self.value)
