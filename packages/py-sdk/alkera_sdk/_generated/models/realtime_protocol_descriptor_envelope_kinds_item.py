from enum import StrEnum


class RealtimeProtocolDescriptorEnvelopeKindsItem(StrEnum):
    ACK = "ack"
    CRDT = "crdt"
    ERROR = "error"
    HELLO = "hello"
    OP = "op"
    PRESENCE = "presence"
    RELOAD = "reload"
    SNAPSHOT = "snapshot"

    def __str__(self) -> str:
        return str(self.value)
