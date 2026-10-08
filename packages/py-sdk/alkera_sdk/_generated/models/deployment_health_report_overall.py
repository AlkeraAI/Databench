from enum import StrEnum


class DeploymentHealthReportOverall(StrEnum):
    FAIL = "fail"
    OK = "ok"
    WARN = "warn"

    def __str__(self) -> str:
        return str(self.value)
