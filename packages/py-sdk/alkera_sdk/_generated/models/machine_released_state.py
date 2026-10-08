from enum import StrEnum


class MachineReleasedState(StrEnum):
    ASLEEP = "asleep"
    BOOTSTRAPPING = "bootstrapping"
    DRAINING = "draining"
    FAILED = "failed"
    LOST = "lost"
    PENDING = "pending"
    PROVISIONING = "provisioning"
    READY = "ready"
    RELEASED = "released"
    RELEASING = "releasing"

    def __str__(self) -> str:
        return str(self.value)
