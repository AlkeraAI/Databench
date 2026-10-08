from enum import StrEnum


class MachineStateReadStatus(StrEnum):
    ASLEEP = "asleep"
    DRAINING = "draining"
    NONE = "none"
    POOL = "pool"
    READY = "ready"
    RESTARTING = "restarting"
    STARTING = "starting"
    UNREACHABLE = "unreachable"

    def __str__(self) -> str:
        return str(self.value)
