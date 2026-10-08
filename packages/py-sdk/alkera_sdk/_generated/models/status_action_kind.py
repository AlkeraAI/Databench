from enum import StrEnum


class StatusActionKind(StrEnum):
    ADD_CREDITS = "add_credits"
    CANCEL_MOVE = "cancel_move"
    CHANGE_MACHINE = "change_machine"
    OPEN_MACHINE = "open_machine"
    RAISE_CAP = "raise_cap"
    REPLACE_MACHINE = "replace_machine"
    RETRY_MOVE = "retry_move"
    SEND_AGAIN = "send_again"
    START_MACHINE = "start_machine"
    WAKE = "wake"

    def __str__(self) -> str:
        return str(self.value)
