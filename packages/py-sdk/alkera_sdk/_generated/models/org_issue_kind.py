from enum import StrEnum


class OrgIssueKind(StrEnum):
    DENIED = "denied"
    ERROR = "error"
    MACHINE_REFUSED = "machine_refused"
    REFUSAL = "refusal"

    def __str__(self) -> str:
        return str(self.value)
