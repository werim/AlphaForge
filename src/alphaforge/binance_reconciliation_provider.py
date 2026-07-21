from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import urllib.parse
from urllib import error, request
from typing import Any, Callable, Mapping

from alphaforge.env_contract import resolve_binance_environment


class ReconciliationAuthError(RuntimeError):
    pass


class ReconciliationPayloadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BinanceReadonlyReconciliationConfig:
    base_url: str
    api_key: str
    api_secret: str
    recv_window_ms: int = 5000
    request_timeout_sec: float = 2.0
    trade_lookback_ms: int = 3_600_000


def load_reconciliation_settings(env: Mapping[str, str]) -> BinanceReadonlyReconciliationConfig:
    """Load the REST-only reconciliation contract independently of websocket runtime."""
    resolved = resolve_binance_environment(env, require_websocket=False)
    return BinanceReadonlyReconciliationConfig(
        base_url=resolved.rest_base_url,
        api_key=str(env.get("BINANCE_API_KEY", "")),
        api_secret=str(env.get("BINANCE_API_SECRET", "")),
        recv_window_ms=int(env.get("BINANCE_RECV_WINDOW_MS") or 5000),
        request_timeout_sec=float(env.get("BINANCE_REQUEST_TIMEOUT_SEC") or 2.0),
        trade_lookback_ms=int(env.get("ALPHAFORGE_BINANCE_RECONCILIATION_TRADE_LOOKBACK_MS") or 3_600_000),
    )


class BinanceReadonlyReconciliationProvider:
    def __init__(
        self,
        *,
        config: BinanceReadonlyReconciliationConfig,
        tracked_symbols: Callable[[], set[str]] | None = None,
        now_ms: Callable[[], int] | None = None,
        http_get_json: Callable[[str, Mapping[str, str], float], Any] | None = None,
    ) -> None:
        if not config.api_key or not config.api_secret:
            raise ReconciliationAuthError("missing_binance_credentials")
        self._cfg = config
        self._tracked_symbols = tracked_symbols or (lambda: set())
        self._now_ms = now_ms or (lambda: int(datetime.now(UTC).timestamp() * 1000))
        self._http_get_json = http_get_json or self._default_http_get_json

    def snapshot(self) -> Mapping[str, Any]:
        retrieved_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            positions_raw = self._signed_get("/fapi/v3/positionRisk", {})
            all_orders_raw = self._signed_get("/fapi/v1/openOrders", {})
            positions = self._normalize_positions(positions_raw)
            orders = self._normalize_orders(all_orders_raw)
            symbols = self._symbols_for_fills(orders, positions)
            fills: list[dict[str, Any]] = []
            for symbol in sorted(symbols):
                fills_raw = self._signed_get("/fapi/v1/userTrades", {"symbol": symbol, "startTime": self._now_ms() - self._cfg.trade_lookback_ms})
                fills.extend(self._normalize_fills(fills_raw))
            return {
                "exchange": "binance",
                "market_type": "USDT_M",
                "retrieved_at": retrieved_at,
                "captured_at": retrieved_at,
                "orders": orders,
                "positions": positions,
                "fills": fills,
                "orphan_orders": len(orders),
                "orphan_positions": len([p for p in positions if abs(float(p.get("qty", 0.0) or 0.0)) > 0.0]),
                "duplicate_fills": 0,
                "evidence_status": "COMPLETE",
                "orphan_coverage": "GLOBAL_OPEN_ORDERS_AND_GLOBAL_POSITION_RISK",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "exchange": "binance",
                "market_type": "USDT_M",
                "retrieved_at": retrieved_at,
                "orders": [],
                "positions": [],
                "fills": [],
                "orphan_orders": 0,
                "orphan_positions": 0,
                "duplicate_fills": 0,
                "evidence_status": "INCOMPLETE",
                "errors": [self._sanitize_error(exc)],
            }

    def _signed_get(self, path: str, params: Mapping[str, Any]) -> Any:
        timestamp = self._now_ms()
        payload = {"timestamp": timestamp, "recvWindow": self._cfg.recv_window_ms, **params}
        query = urllib.parse.urlencode(payload)
        signature = hmac.new(self._cfg.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"{self._cfg.base_url.rstrip('/')}{path}?{query}&signature={signature}"
        headers = {"X-MBX-APIKEY": self._cfg.api_key}
        try:
            return self._http_get_json(url, headers, self._cfg.request_timeout_sec)
        except error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise ReconciliationAuthError(f"binance_auth_failed_status_{exc.code}") from exc
            raise

    @staticmethod
    def _default_http_get_json(url: str, headers: Mapping[str, str], timeout_sec: float) -> Any:
        req = request.Request(url, method="GET", headers=dict(headers))
        with request.urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _normalize_orders(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise ReconciliationPayloadError("open_orders_payload_not_list")
        out: list[dict[str, Any]] = []
        for row in payload:
            if not isinstance(row, Mapping):
                raise ReconciliationPayloadError("open_orders_row_not_object")
            out.append({
                "order_id": str(row.get("orderId") or ""),
                "symbol": str(row.get("symbol") or ""),
                "status": str(row.get("status") or "UNKNOWN"),
                "side": str(row.get("side") or ""),
                "position_side": str(row.get("positionSide") or "BOTH"),
                "price": float(row.get("price", 0.0) or 0.0),
                "qty": float(row.get("origQty", 0.0) or 0.0),
                "executed_qty": float(row.get("executedQty", 0.0) or 0.0),
                "created_at": datetime.fromtimestamp(int(row.get("time", 0) or 0) / 1000, tz=UTC).isoformat().replace("+00:00", "Z"),
            })
        return out

    @staticmethod
    def _normalize_positions(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise ReconciliationPayloadError("position_risk_payload_not_list")
        out: list[dict[str, Any]] = []
        for row in payload:
            if not isinstance(row, Mapping):
                raise ReconciliationPayloadError("position_risk_row_not_object")
            qty = float(row.get("positionAmt", 0.0) or 0.0)
            out.append({
                "symbol": str(row.get("symbol") or ""),
                "qty": qty,
                "entry_price": float(row.get("entryPrice", 0.0) or 0.0),
                "position_side": str(row.get("positionSide") or "BOTH"),
                "unrealized_pnl": float(row.get("unRealizedProfit", 0.0) or 0.0),
            })
        return out

    @staticmethod
    def _normalize_fills(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise ReconciliationPayloadError("user_trades_payload_not_list")
        out: list[dict[str, Any]] = []
        for row in payload:
            if not isinstance(row, Mapping):
                raise ReconciliationPayloadError("user_trades_row_not_object")
            out.append({
                "trade_id": str(row.get("id") or ""),
                "order_id": str(row.get("orderId") or ""),
                "symbol": str(row.get("symbol") or ""),
                "side": "SELL" if bool(row.get("buyer")) is False else "BUY",
                "position_side": str(row.get("positionSide") or "BOTH"),
                "qty": float(row.get("qty", 0.0) or 0.0),
                "price": float(row.get("price", 0.0) or 0.0),
                "realized_pnl": float(row.get("realizedPnl", 0.0) or 0.0),
                "time": int(row.get("time", 0) or 0),
            })
        return out

    def _symbols_for_fills(self, orders: list[dict[str, Any]], positions: list[dict[str, Any]]) -> set[str]:
        symbols = set(self._tracked_symbols())
        symbols.update({str(o.get("symbol") or "") for o in orders if o.get("symbol")})
        symbols.update({str(p.get("symbol") or "") for p in positions if abs(float(p.get("qty", 0.0) or 0.0)) > 0.0})
        return {s for s in symbols if s}

    @staticmethod
    def _sanitize_error(exc: Exception) -> str:
        return f"{exc.__class__.__name__}:request_failed_redacted"
