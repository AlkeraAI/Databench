from enum import StrEnum


class ComputeAllocationInfoMachineStatusType0(StrEnum):
    ASLEEP = "asleep"
    DRAINING = "draining"
    READY = "ready"
    RESTARTING = "restarting"
    STARTING = "starting"
    UNREACHABLE = "unreachable"

    def __str__(self) -> str:
        return str(self.value)
