from enum import StrEnum


class StockReadState(StrEnum):
    IN_STOCK = "in_stock"
    LIMITED = "limited"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
