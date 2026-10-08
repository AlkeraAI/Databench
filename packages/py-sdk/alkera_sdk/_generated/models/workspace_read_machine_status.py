from enum import StrEnum


class WorkspaceReadMachineStatus(StrEnum):
    ASLEEP = "asleep"
    DRAINING = "draining"
    NONE = "none"
    READY = "ready"
    REFUSED = "refused"
    STARTING = "starting"
    STRANDED = "stranded"
    UNREACHABLE = "unreachable"

    def __str__(self) -> str:
        return str(self.value)
