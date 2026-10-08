from enum import StrEnum


class ChatSessionReadSessionState(StrEnum):
    ASLEEP = "asleep"
    AWAKE = "awake"
    QUEUED = "queued"
    STARTING = "starting"
    WAKING = "waking"
    WORKING = "working"

    def __str__(self) -> str:
        return str(self.value)
