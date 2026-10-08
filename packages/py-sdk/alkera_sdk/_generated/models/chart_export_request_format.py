from enum import StrEnum


class ChartExportRequestFormat(StrEnum):
    PNG = "png"
    SVG = "svg"

    def __str__(self) -> str:
        return str(self.value)
