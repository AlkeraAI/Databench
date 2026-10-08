from enum import StrEnum


class OpsHealthCheckStatus(StrEnum):
    FAIL = "fail"
    OK = "ok"
    SKIPPED = "skipped"
    WARN = "warn"

    def __str__(self) -> str:
        return str(self.value)
