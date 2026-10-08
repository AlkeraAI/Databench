from enum import StrEnum


class Reauth(StrEnum):
    ADMIN = "admin"
    BROWSER = "browser"
    EXTERNAL_CLI = "external_cli"
    REENTER = "reenter"

    def __str__(self) -> str:
        return str(self.value)
