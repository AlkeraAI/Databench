from enum import StrEnum


class DeploymentHealthCheckReadStatus(StrEnum):
    FAIL = "fail"
    OK = "ok"
    SKIPPED = "skipped"
    WARN = "warn"

    def __str__(self) -> str:
        return str(self.value)
