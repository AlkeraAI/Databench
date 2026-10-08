from enum import StrEnum


class QueuedRunTrigger(StrEnum):
    AUTORUN = "autorun"
    RUN = "run"
    RUN_ALL = "run_all"
    RUN_STALE = "run_stale"
    WIDGET = "widget"

    def __str__(self) -> str:
        return str(self.value)
