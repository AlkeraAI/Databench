from enum import StrEnum


class LiveFacetStateType0(StrEnum):
    DEFERRED = "deferred"
    INBOUND = "inbound"
    INBOUND_DELETE = "inbound_delete"
    INBOUND_RENAME = "inbound_rename"
    ON_BOX = "on_box"
    UPLOADING = "uploading"
    WRITING = "writing"

    def __str__(self) -> str:
        return str(self.value)
