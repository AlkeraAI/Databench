from enum import StrEnum


class FleetMachineRowStatus(StrEnum):
    ASLEEP = "asleep"
    DRAINING = "draining"
    NONE = "none"
    READY = "ready"
    RESTARTING = "restarting"
    STARTING = "starting"
    UNREACHABLE = "unreachable"

    def __str__(self) -> str:
        return str(self.value)
