from enum import StrEnum


class Badge(StrEnum):
    CONNECTED = "connected"
    DISABLED = "disabled"
    ERROR = "error"
    INCOMPLETE = "incomplete"
    MUTED = "muted"
    NEEDS_REAUTH = "needs_reauth"
    NOT_CHECKED = "not_checked"
    NO_ACCESS = "no_access"
    STALE = "stale"
    UNREACHABLE = "unreachable"
    UNSUPPORTED = "unsupported"
    VERIFYING = "verifying"

    def __str__(self) -> str:
        return str(self.value)
