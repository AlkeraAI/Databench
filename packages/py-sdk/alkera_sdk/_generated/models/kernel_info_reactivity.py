from enum import StrEnum


class KernelInfoReactivity(StrEnum):
    AUTORUN = "autorun"
    LAZY = "lazy"

    def __str__(self) -> str:
        return str(self.value)
