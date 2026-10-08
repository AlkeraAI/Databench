from enum import StrEnum


class ChatPublisherStateUpdateState(StrEnum):
    ASLEEP = "asleep"
    PUBLISHING = "publishing"
    REFUSED = "refused"
    WAITING = "waiting"

    def __str__(self) -> str:
        return str(self.value)
