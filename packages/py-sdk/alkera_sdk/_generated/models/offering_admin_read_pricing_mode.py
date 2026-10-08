from enum import StrEnum


class OfferingAdminReadPricingMode(StrEnum):
    FIXED = "fixed"
    PASS_THROUGH = "pass_through"

    def __str__(self) -> str:
        return str(self.value)
