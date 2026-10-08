from enum import StrEnum


class PlatformRole(StrEnum):
    ALKERA_ADMIN = "alkera_admin"
    ALKERA_SUPPORT = "alkera_support"

    def __str__(self) -> str:
        return str(self.value)
