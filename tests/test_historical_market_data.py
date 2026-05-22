import json

import pytest

from alphaforge.historical_market_data import (
    HistoricalDataError,
    build_cache_metadata,
    cache_covers,
    fetch_binance_klines_paginated,
    join_funding_to_candles,
)


def test_paginated_klines_and_dedupe() -> None:
    step = 60_000
    p1 = [[0,1,1,1,1,1],[step,1,1,1,1,1],[2*step,1,1,1,1,1]]
    p2 = [[2*step,1,1,1,1,1],[3*step,1,1,1,1,1],[4*step,1,1,1,1,1]]
    pages = [p1, p2]
    def fetcher(_url: str):
        return pages.pop(0) if pages else []
    rows = fetch_binance_klines_paginated("BTCUSDT", "1m", 0, 4*step, fetcher=fetcher)
    assert len(rows) == 5
    assert [r.timestamp for r in rows] == [0, step, 2*step, 3*step, 4*step]


def test_incomplete_coverage_fails() -> None:
    with pytest.raises(HistoricalDataError):
        fetch_binance_klines_paginated("BTCUSDT", "1m", 0, 180_000, fetcher=lambda _u: [[0,1,1,1,1,1]])


def test_cache_coverage_detection() -> None:
    md = {"actual_first_ts": 100, "actual_last_ts": 300}
    assert cache_covers(md, 120, 280)
    assert not cache_covers(md, 90, 280)


def test_funding_join_no_future_leak() -> None:
    candles = fetch_binance_klines_paginated("BTCUSDT", "1m", 0, 120_000, fetcher=lambda _u: [[0,1,1,1,1,1],[60_000,1,1,1,1,1],[120_000,1,1,1,1,1]])
    joined = join_funding_to_candles(candles, [(60_000, 0.001), (180_000, 0.002)])
    assert joined[0].funding_rate_pct is None
    assert joined[1].funding_rate_pct == pytest.approx(0.001)
    assert joined[2].funding_rate_pct == pytest.approx(0.001)
