from typing import Protocol

from alphaforge.clients.binance import SymbolInfo


class SymbolDiscoveryContract(Protocol):
    def discover(self) -> list[SymbolInfo]:
        ...
