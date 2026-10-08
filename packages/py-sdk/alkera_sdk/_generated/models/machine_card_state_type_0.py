from enum import StrEnum


class MachineCardStateType0(StrEnum):
    DELETED = "deleted"
    FAILED = "failed"
    RUNNING = "running"
    STARTING = "starting"
    STOPPED = "stopped"
    STOPPING = "stopping"
    UNHEALTHY = "unhealthy"
    UNREACHABLE = "unreachable"
    WAITING_FOR_HARDWARE = "waiting_for_hardware"

    def __str__(self) -> str:
        return str(self.value)
