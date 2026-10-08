from enum import StrEnum


class KernelRequestAction(StrEnum):
    INTERRUPT = "interrupt"
    INTERRUPT_ALL = "interrupt_all"
    RESTART = "restart"
    SHUTDOWN = "shutdown"
    STATUS = "status"

    def __str__(self) -> str:
        return str(self.value)
