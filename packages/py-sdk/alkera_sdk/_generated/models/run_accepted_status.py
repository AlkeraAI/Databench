from enum import StrEnum


class RunAcceptedStatus(StrEnum):
    COALESCED = "coalesced"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    KERNEL_RESTARTED = "kernel_restarted"
    NEEDS_CONFIRMATION = "needs_confirmation"
    OK = "ok"
    PLANNED = "planned"
    QUEUED = "queued"
    REFUSED = "refused"
    RUNNING = "running"

    def __str__(self) -> str:
        return str(self.value)
