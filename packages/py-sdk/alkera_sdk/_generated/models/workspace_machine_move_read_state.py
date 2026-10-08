from enum import StrEnum


class WorkspaceMachineMoveReadState(StrEnum):
    CANCELED = "canceled"
    DONE = "done"
    DRAINING = "draining"
    FAILED = "failed"
    REQUESTED = "requested"
    SWITCHING = "switching"
    WAKING = "waking"

    def __str__(self) -> str:
        return str(self.value)
