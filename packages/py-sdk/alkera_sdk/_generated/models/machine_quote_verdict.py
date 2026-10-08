from enum import StrEnum


class MachineQuoteVerdict(StrEnum):
    OK = "ok"
    REFUSED = "refused"

    def __str__(self) -> str:
        return str(self.value)
