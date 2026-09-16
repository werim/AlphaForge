"""Regression coverage for closed-candle resolution and PAPER dollar accounting."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from urllib import error
from urllib.parse import parse_qs, urlparse

import pytest

from alphaforge.burnin_campaign import (
    BinanceReadOnlyCandleProvider, BurnInCampaignRunner, MarketDataImmature,
    ProviderFailure, create_campaign, start_or_resume_campaign,
)
from alphaforge.burnin_resolver import persist_pending_position, resolve_position_closure
from alphaforge.historical_market_data import expected_candle_count
from alphaforge.persistence import init_db
from alphaforge.provider_failures import classify_provider_exception
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def _rows(url: str, *, omit_last: bool = False):
    query = parse_qs(urlparse(url).query)
    first = int(query["startTime"][0])
    last = int(query["endTime"][0])
    first = ((first + 59_999) // 60_000) * 60_000
    stamps = list(range(first, last + 1, 60_000))
    if omit_last:
        stamps = stamps[:-1]
    return [[stamp, 100, 101, 99, 100, 1] for stamp in stamps]


def test_exact_unclosed_boundary_requires_only_four_closed_minutes():
    start = datetime.fromisoformat("2026-09-15T19:45:07+00:00")
    end = datetime.fromisoformat("2026-09-15T19:50:00+00:00")
    assert expected_candle_count(int(start.timestamp() * 1000) + 1,
                                 int(end.timestamp() * 1000), "1m", closed_only=True) == 4
    urls = []
    provider = BinanceReadOnlyCandleProvider(fetcher=lambda url: urls.append(url) or _rows(url))
    candles = provider("BTCUSDT", "2026-09-15T19:45:07Z", "2026-09-15T19:50:00Z")
    assert [c["timestamp"] for c in candles] == [
        f"2026-09-15T19:{minute}:00Z" for minute in ("46", "47", "48", "49")]
    assert parse_qs(urlparse(urls[0]).query)["endTime"] == [str(int(end.timestamp() * 1000) - 60_000)]


def test_provider_discards_current_candle_even_if_exchange_returns_it():
    def fetcher(url):
        rows = _rows(url)
        if rows:
            rows.append([rows[-1][0] + 60_000, 100, 101, 99, 100, 1])
        return rows
    provider = BinanceReadOnlyCandleProvider(fetcher=fetcher)
    candles = provider("BTCUSDT", "2026-09-15T19:45:07Z", "2026-09-15T19:50:00Z")
    assert len(candles) == 4
    assert candles[-1]["timestamp"] == "2026-09-15T19:49:00Z"


def test_recent_closed_candle_lag_is_retryable_and_auth_transport_keep_classes():
    now = datetime.now(timezone.utc)
    end = now.replace(second=0, microsecond=0)
    start = end - timedelta(minutes=5)
    provider = BinanceReadOnlyCandleProvider(fetcher=lambda url: _rows(url, omit_last=True))
    with pytest.raises(MarketDataImmature) as exc:
        provider("BTCUSDT", start.isoformat(), end.isoformat())
    assert classify_provider_exception(exc.value) == "RETRYABLE_MARKET_DATA"
    auth = ProviderFailure("PROVIDER_FAILURE:HTTPError")
    auth.__cause__ = error.HTTPError("https://fapi.binance.com", 401, "auth", None, None)
    transport = ProviderFailure("PROVIDER_FAILURE:URLError")
    transport.__cause__ = error.URLError(TimeoutError("timeout"))
    assert classify_provider_exception(auth) == "PERMANENT_AUTH_OR_PROTOCOL"
    assert classify_provider_exception(transport) == "TRANSIENT_TRANSPORT"


def test_immature_candle_never_pauses_campaign_or_accumulates_failure_threshold(tmp_path):
    db = tmp_path / "campaign.db"
    engine = init_db(f"sqlite+pysqlite:///{db}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    campaign = create_campaign(conn, release_id="test", duration_days=1,
                               symbols=["BTCUSDT"], intervals=["1m"])
    run = start_or_resume_campaign(conn, campaign.campaign_id)
    entry = datetime.now(timezone.utc) - timedelta(minutes=5)
    persist_pending_position(conn, trade_id="open", campaign_id=campaign.campaign_id,
        burnin_run_id=run["burnin_run_id"], signal_id="s", symbol="BTCUSDT",
        side="LONG", entry_time=entry.isoformat(), planned_entry=100,
        simulated_fill=100, stop=90, target=120, quantity=.1, notional=10,
        entry_spread=0, entry_slippage=0, entry_fee=0, regime="TREND",
        source_provenance={})
    conn.commit()
    provider = BinanceReadOnlyCandleProvider(fetcher=lambda url: _rows(url, omit_last=True))
    runner = BurnInCampaignRunner(engine, campaign.campaign_id, provider,
                                  resolver_failure_threshold=2)
    for _ in range(4):
        result = runner.resolver_tick()
        assert result["status"] == "RETRYING"
        assert result["defer_reason"] == "IMMATURE_CANDLE"
    assert runner.resolver_failure_count == 0
    assert conn.execute("SELECT campaign_status FROM burnin_campaigns WHERE campaign_id=?",
                        (campaign.campaign_id,)).fetchone()[0] == "RUNNING"
    assert conn.execute("SELECT status FROM burnin_pending_position_outcomes WHERE trade_id='open'").fetchone()[0] == "OPEN"
    conn.close()
    engine.dispose()


class _Brain:
    session = None


async def _scanner():
    return []


@pytest.mark.parametrize("symbol,fill,move", [
    ("BTCUSDT", 76002.0, 108.91),
    ("ETHUSDT", 2404.0, 4.61),
])
def test_paper_notional_quantity_pnl_costs_and_r_agree(tmp_path, symbol, fill, move):
    db = tmp_path / "paper.db"
    engine = init_db(f"sqlite+pysqlite:///{db}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    campaign = create_campaign(conn, release_id="test", duration_days=1,
                               symbols=[symbol], intervals=["1m"])
    run = start_or_resume_campaign(conn, campaign.campaign_id)
    conn.commit()
    runtime = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        paper_candidate_notional=10.0), _Brain(), _scanner, persistence_engine=engine)
    runtime._campaign_id = campaign.campaign_id
    runtime._burnin_run_id = run["burnin_run_id"]
    market = {"entry": fill, "sl": fill - 100, "tp": fill + move,
              "side": "LONG", "quantity": 1.0, "notional": 10.0,
              "execution_ctx": {"spread_pct": .0002, "expected_slippage_pct": .0002,
                                "latency_ms": 0, "funding_rate_pct": 0,
                                "fee_pct": .0004, "liquidity_score": 1,
                                "volatility_regime": "normal"}}
    runtime._persist_pending_paper_position(symbol, "trade", {"signal_id": "s"},
                                             market, {"fill_price": fill})
    position = conn.execute("SELECT * FROM burnin_pending_position_outcomes WHERE trade_id='trade'").fetchone()
    quantity = 10.0 / fill
    risk_usd = 100 * quantity
    assert position["quantity"] == pytest.approx(quantity)
    assert position["notional"] == pytest.approx(position["quantity"] * position["simulated_fill"])
    assert position["entry_spread"] < .01
    exit_costs = {"exit_spread": .005, "exit_slippage": .005,
                  "exit_fee": .005, "funding": 0, "latency_impact_penalty": 0,
                  "volatility_penalty": 0, "liquidity_penalty": 0}
    resolve_position_closure(conn, trade_id="trade", exit_time="2026-09-16T00:00:00Z",
                             exit_price=fill + move, exit_reason="TP_HIT",
                             exit_costs=exit_costs)
    pending = conn.execute("SELECT * FROM burnin_pending_position_outcomes WHERE trade_id='trade'").fetchone()
    outcome = conn.execute("SELECT * FROM burnin_trade_outcomes WHERE trade_id='trade'").fetchone()
    expected_gross = move * quantity
    assert pending["gross_pnl"] == pytest.approx(expected_gross)
    assert outcome["gross_pnl"] == pytest.approx(expected_gross)
    assert expected_gross < .03  # The same move on one full coin would be dollars.
    assert pending["total_execution_cost"] == pytest.approx(outcome["total_execution_cost"])
    assert pending["net_pnl"] == pytest.approx(pending["gross_pnl"] - pending["total_execution_cost"])
    assert pending["net_r"] == pytest.approx(pending["net_pnl"] / risk_usd)
    assert outcome["net_pnl"] == pytest.approx(pending["net_pnl"])
    assert outcome["net_r"] == pytest.approx(pending["net_r"])
    assert (pending["net_pnl"] > 0) == (pending["net_r"] > 0)
    assert (pending["net_pnl"] < 0) == (symbol == "BTCUSDT")
    conn.close()
    engine.dispose()


def test_legacy_open_position_r_costs_convert_to_dollars_at_closure(tmp_path):
    db = tmp_path / "legacy-open.db"
    engine = init_db(f"sqlite+pysqlite:///{db}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    campaign = create_campaign(conn, release_id="test", duration_days=1,
                               symbols=["BTCUSDT"], intervals=["1m"])
    run = start_or_resume_campaign(conn, campaign.campaign_id)
    persist_pending_position(conn, trade_id="legacy", campaign_id=campaign.campaign_id,
        burnin_run_id=run["burnin_run_id"], signal_id="s", symbol="BTCUSDT",
        side="LONG", entry_time="2026-09-15T19:45:00Z", planned_entry=100,
        simulated_fill=100, stop=90, target=120, quantity=.1, notional=10,
        entry_spread=.1, entry_slippage=.1, entry_fee=.1, regime="TREND",
        source_provenance={"execution_cost_unit": "R"})
    resolve_position_closure(conn, trade_id="legacy", exit_time="2026-09-15T19:50:00Z",
        exit_price=101, exit_reason="TP_HIT", exit_costs={
            "exit_spread": .1, "exit_slippage": .1, "exit_fee": .1,
            "funding": 0, "latency_impact_penalty": 0,
            "volatility_penalty": 0, "liquidity_penalty": 0})
    pending = conn.execute("SELECT * FROM burnin_pending_position_outcomes WHERE trade_id='legacy'").fetchone()
    outcome = conn.execute("SELECT * FROM burnin_trade_outcomes WHERE trade_id='legacy'").fetchone()
    assert pending["gross_pnl"] == pytest.approx(.1)
    assert pending["total_execution_cost"] == pytest.approx(.6)
    assert outcome["total_execution_cost"] == pytest.approx(.6)
    assert pending["net_pnl"] == pytest.approx(-.5)
    assert pending["net_r"] == pytest.approx(-.5)
    conn.close()
    engine.dispose()
