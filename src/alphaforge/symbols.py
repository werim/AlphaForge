from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,30}$")
_SPLIT_RE = re.compile(r"[\s,]+")


class SymbolListError(ValueError):
    """Raised when a user-provided symbol list cannot be safely fetched."""


def normalize_symbol_list(value: Any) -> list[str]:
    """Normalize CLI/dashboard symbol input into unique Binance symbol tokens.

    Splits on commas and whitespace, trims, uppercases, drops empty tokens, and
    deduplicates while preserving order. Plus signs and other separators are not
    accepted because Binance endpoints require exactly one symbol per request.
    """
    raw_parts: list[str] = []
    if value is None:
        return []
    if isinstance(value, str):
        raw_parts = [value]
    elif isinstance(value, Iterable):
        raw_parts = [str(item) for item in value]
    else:
        raw_parts = [str(value)]

    symbols: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw in raw_parts:
        for token in _SPLIT_RE.split(str(raw).strip()):
            symbol = token.strip().upper()
            if not symbol:
                continue
            if not _SYMBOL_RE.fullmatch(symbol):
                invalid.append(token)
                continue
            if symbol not in seen:
                symbols.append(symbol)
                seen.add(symbol)
    if invalid:
        got = ",".join(str(part) for part in raw_parts)
        raise SymbolListError(f"Invalid symbol list: expected symbols like BTCUSDT,ETHUSDT; got {got}")
    return symbols


def validate_single_binance_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    if not normalized or not _SYMBOL_RE.fullmatch(normalized):
        raise SymbolListError(f"Invalid symbol list: expected symbols like BTCUSDT,ETHUSDT; got {symbol}")
    return normalized
