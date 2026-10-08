from enum import StrEnum


class OfferingCreatePricingMode(StrEnum):
    FIXED = "fixed"
    PASS_THROUGH = "pass_through"

    def __str__(self) -> str:
        return str(self.value)
