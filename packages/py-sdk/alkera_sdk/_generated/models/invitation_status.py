from enum import StrEnum


class InvitationStatus(StrEnum):
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    PENDING = "pending"
    REJECTED = "rejected"
    REVOKED = "revoked"

    def __str__(self) -> str:
        return str(self.value)
