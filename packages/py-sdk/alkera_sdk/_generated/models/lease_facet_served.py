from enum import StrEnum


class LeaseFacetServed(StrEnum):
    LIVE = "live"
    OFFLINE = "offline"

    def __str__(self) -> str:
        return str(self.value)
