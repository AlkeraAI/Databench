from enum import StrEnum


class StatusFactTone(StrEnum):
    DANGER = "danger"
    INFO = "info"
    MUTED = "muted"
    NEUTRAL = "neutral"
    SUCCESS = "success"
    WARNING = "warning"

    def __str__(self) -> str:
        return str(self.value)
