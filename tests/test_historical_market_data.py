import json

import pytest

from alphaforge.historical_market_data import (
    HistoricalDataError,
    build_cache_metadata,
    cache_covers,
    expected_candle_count,
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


def test_paginated_klines_honors_read_only_market_data_base_url() -> None:
    calls = []
    rows = fetch_binance_klines_paginated(
        "BTCUSDT", "1m", 0, 0,
        fetcher=lambda url: calls.append(url) or [[0, 1, 1, 1, 1, 1]],
        base_url="https://market-data.example/",
    )
    assert len(rows) == 1
    assert calls[0].startswith("https://market-data.example/fapi/v1/klines?")


def test_incomplete_coverage_fails() -> None:
    pages = iter([[[0,1,1,1,1,1]], []])
    with pytest.raises(HistoricalDataError) as exc:
        fetch_binance_klines_paginated("BTCUSDT", "1m", 0, 180_000, fetcher=lambda _url: next(pages))
    message = str(exc.value)
    assert "symbol=BTCUSDT" in message
    assert "timeframe=1m" in message
    assert "expected_candles=4" in message
    assert "actual_candles=1" in message


def test_thirty_days_one_minute_requires_pagination() -> None:
    step = 60_000
    start_ms = 0
    end_ms = (30 * 24 * 60 * step) - 1
    assert expected_candle_count(start_ms, end_ms, "1m") == 43_200
    calls = []

    def fetcher(url: str):
        calls.append(url)
        page_start = int(url.split("startTime=")[1].split("&")[0])
        rows = []
        for i in range(1500):
            ts = page_start + i * step
            if ts > end_ms:
                break
            rows.append([ts, 1, 1, 1, 1, 1])
        return rows

    rows = fetch_binance_klines_paginated("BTCUSDT", "1m", start_ms, end_ms, fetcher=fetcher)
    assert len(rows) == 43_200
    assert len(calls) > 1
    assert all("limit=1500" in call for call in calls)


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


def test_stale_cache_fetches_before_raising_historical_data_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import alphaforge.historical_market_data as hmd

    cache_path = tmp_path / "candles" / "BTCUSDT_1m.json"
    cached = [hmd.HistoricalCandle(60_000, 1, 1, 1, 1, 1)]
    hmd.write_cache(
        cache_path,
        cached,
        {"actual_first_ts": 60_000, "actual_last_ts": 60_000},
    )
    calls = {"fetch": 0}

    def fake_fetch(**kwargs):
        calls["fetch"] += 1
        raise hmd.HistoricalDataError("Historical coverage starts after requested start")

    monkeypatch.setattr(hmd, "fetch_binance_klines_paginated", fake_fetch)
    with pytest.raises(HistoricalDataError):
        hmd.load_or_fetch_candles("BTCUSDT", "1m", 0, 120_000, tmp_path)
    assert calls["fetch"] == 1


def test_interval_ms_supports_daily_and_four_hour() -> None:
    from alphaforge.historical_market_data import _interval_ms

    assert _interval_ms("1d") == 86_400_000
    assert _interval_ms("4h") == 14_400_000


def test_daily_paginated_klines_advances_by_one_day() -> None:
    step = 86_400_000
    calls: list[str] = []

    def fetcher(url: str):
        calls.append(url)
        page_start = int(url.split("startTime=")[1].split("&")[0])
        return [[page_start, 1, 1, 1, 1, 1], [page_start + step, 1, 1, 1, 1, 1]]

    rows = fetch_binance_klines_paginated("BTCUSDT", "1d", 0, 3 * step, fetcher=fetcher)
    assert [r.timestamp for r in rows] == [0, step, 2 * step, 3 * step]
    assert "startTime=0" in calls[0]
    assert f"startTime={2 * step}" in calls[1]


def test_unsupported_interval_is_not_not_enough_data() -> None:
    with pytest.raises(HistoricalDataError) as exc:
        fetch_binance_klines_paginated("BTCUSDT", "2d", 0, 86_400_000, fetcher=lambda _url: [])
    message = str(exc.value)
    assert "UNSUPPORTED_TIMEFRAME" in message
    assert "requested_interval=2d" in message
    assert "source_function=fetch_binance_klines_paginated" in message
    assert "NOT_ENOUGH_HISTORICAL_DATA" not in message


def test_symbol_list_normalization_accepts_comma_and_dedupes() -> None:
    from alphaforge.symbols import normalize_symbol_list

    assert normalize_symbol_list("BTCUSDT, ethusdt, BTCUSDT") == ["BTCUSDT", "ETHUSDT"]


def test_symbol_list_normalization_accepts_quoted_comma_and_whitespace() -> None:
    from alphaforge.symbols import normalize_symbol_list

    assert normalize_symbol_list(["BTCUSDT,ETHUSDT", " solusdt "]) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert normalize_symbol_list("BTCUSDT ETHUSDT") == ["BTCUSDT", "ETHUSDT"]


def test_symbol_list_normalization_rejects_plus_combined_symbol() -> None:
    from alphaforge.symbols import SymbolListError, normalize_symbol_list

    with pytest.raises(SymbolListError) as exc:
        normalize_symbol_list("BTCUSDT+ETHUSDT")
    assert "Invalid symbol list: expected symbols like BTCUSDT,ETHUSDT; got BTCUSDT+ETHUSDT" in str(exc.value)


def test_load_or_fetch_candles_rejects_combined_symbol_before_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import alphaforge.historical_market_data as hmd
    from alphaforge.symbols import SymbolListError

    called = {"fetch": False}

    def fake_fetch(**_kwargs):
        called["fetch"] = True
        return []

    monkeypatch.setattr(hmd, "fetch_binance_klines_paginated", fake_fetch)
    with pytest.raises(SymbolListError):
        hmd.load_or_fetch_candles("BTCUSDT,ETHUSDT", "1m", 0, 60_000, tmp_path)
    assert called["fetch"] is False
