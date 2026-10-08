from enum import StrEnum


class OfferingCreateAudience(StrEnum):
    ALL = "all"
    ENTERPRISE = "enterprise"
    LISTED = "listed"

    def __str__(self) -> str:
        return str(self.value)
