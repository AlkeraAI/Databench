from enum import StrEnum


class Outcome(StrEnum):
    ERROR = "error"
    INFRASTRUCTURE = "infrastructure"
    INVALID_CREDENTIAL = "invalid_credential"
    OK = "ok"
    PERMISSION = "permission"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    UNSUPPORTED = "unsupported"

    def __str__(self) -> str:
        return str(self.value)
