from enum import StrEnum


class VerificationState(StrEnum):
    ABANDONED = "abandoned"
    QUEUED = "queued"
    RUNNING = "running"
    SETTLED = "settled"

    def __str__(self) -> str:
        return str(self.value)
