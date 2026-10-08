from enum import StrEnum


class ChatModelOptionReasonCodeType0(StrEnum):
    HARNESS_WIRE_UNSUPPORTED = "harness_wire_unsupported"
    MODEL_NOT_OFFERED = "model_not_offered"
    REASONING_NOT_READABLE = "reasoning_not_readable"

    def __str__(self) -> str:
        return str(self.value)
