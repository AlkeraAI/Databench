from enum import StrEnum


class CrashReportCreateComponent(StrEnum):
    BACKEND = "backend"
    CLI = "cli"
    DAEMON = "daemon"
    EXTENSION = "extension"
    GATEWAY = "gateway"
    WEB = "web"

    def __str__(self) -> str:
        return str(self.value)
