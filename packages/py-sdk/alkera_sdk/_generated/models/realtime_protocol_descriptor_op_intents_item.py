from enum import StrEnum


class RealtimeProtocolDescriptorOpIntentsItem(StrEnum):
    APPEND = "append"
    CHUNK = "chunk"
    SET_FIELDS = "set_fields"
    SET_META = "set_meta"
    USER_MESSAGE = "user_message"

    def __str__(self) -> str:
        return str(self.value)
