from enum import StrEnum


class LeaseFacetPurpose(StrEnum):
    BOX = "box"
    CHAT = "chat"
    MOUNT = "mount"
    SHARE = "share"
    WORKSPACE = "workspace"

    def __str__(self) -> str:
        return str(self.value)
