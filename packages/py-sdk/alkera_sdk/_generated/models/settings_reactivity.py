from enum import StrEnum


class SettingsReactivity(StrEnum):
    AUTORUN = "autorun"
    LAZY = "lazy"

    def __str__(self) -> str:
        return str(self.value)
