from enum import StrEnum


class CellAfterOpStatusType0(StrEnum):
    DISABLED = "disabled"
    EDITED = "edited"
    ERROR = "error"
    FRESH = "fresh"
    INTERRUPTED = "interrupted"
    NOT_RUN = "not_run"
    QUEUED = "queued"
    RUNNING = "running"
    SKIPPED = "skipped"
    STALE = "stale"
    STOPPED = "stopped"

    def __str__(self) -> str:
        return str(self.value)
