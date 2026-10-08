from enum import StrEnum


class KernelInfoState(StrEnum):
    ABSENT = "absent"
    BUSY = "busy"
    IDLE = "idle"
    RESTARTING = "restarting"
    STARTING = "starting"
    STOPPED = "stopped"

    def __str__(self) -> str:
        return str(self.value)
