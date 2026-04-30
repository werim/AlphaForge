from dataclasses import dataclass
from typing import Protocol

from alphaforge.models.schema import MarketType


@dataclass(frozen=True)
class SymbolInfo:
    exchange: str
    symbol: str
    base_asset: str
    quote_asset: str
    market_type: MarketType


class BinanceMarketClient(Protocol):
    market_type: MarketType

    def fetch_symbols(self) -> list[SymbolInfo]:
        ...


class BinanceUSDTMClient:
    market_type = MarketType.USDT_M

    def fetch_symbols(self) -> list[SymbolInfo]:
        return []


class BinanceCoinMClient:
    market_type = MarketType.COIN_M

    def fetch_symbols(self) -> list[SymbolInfo]:
        return []
