from enum import StrEnum


class ChatModelOptionsApplies(StrEnum):
    AFTER_REOPEN = "after_reopen"
    NEXT_TURN = "next_turn"

    def __str__(self) -> str:
        return str(self.value)
