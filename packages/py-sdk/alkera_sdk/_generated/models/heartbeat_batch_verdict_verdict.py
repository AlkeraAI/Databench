from enum import StrEnum


class HeartbeatBatchVerdictVerdict(StrEnum):
    GONE = "gone"
    RENEWED = "renewed"
    SUPERSEDED = "superseded"

    def __str__(self) -> str:
        return str(self.value)
