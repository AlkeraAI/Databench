from enum import StrEnum


class ChatWakeReadOutcome(StrEnum):
    AWAKE = "awake"
    MACHINE_UNAVAILABLE = "machine_unavailable"
    THROTTLED = "throttled"
    WAKING = "waking"

    def __str__(self) -> str:
        return str(self.value)
