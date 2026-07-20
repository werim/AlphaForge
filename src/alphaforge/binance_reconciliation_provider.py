from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import http.client
import io
import json
import random
import socket
import ssl
import time
from urllib import error
import urllib.parse
from typing import Any, Callable, Mapping

class ReconciliationAuthError(RuntimeError):
    pass


class ReconciliationPayloadError(RuntimeError):
    pass


class ReconciliationRequestError(RuntimeError):
    def __init__(self, *, endpoint_class: str, environment: str, symbol: str | None = None, http_status: int | None = None, binance_code: int | None = None, retry_count: int = 0, timeout_category: str | None = None, reason: str = "request_failed") -> None:
        super().__init__(reason)
        self.diagnostic = {
            "reason": reason,
            "endpoint_class": endpoint_class,
            "symbol": symbol,
            "http_status": http_status,
            "binance_code": binance_code,
            "retry_count": retry_count,
            "timeout_category": timeout_category,
            "environment": environment,
        }


@dataclass(frozen=True, slots=True)
class BinanceReadonlyReconciliationConfig:
    base_url: str
    api_key: str
    api_secret: str
    recv_window_ms: int = 5000
    request_timeout_sec: float = 2.0
    trade_lookback_ms: int = 3_600_000
    position_epsilon: float = 1e-8
    max_fill_symbols: int = 20
    max_retries: int = 2


class BinanceReadonlyReconciliationProvider:
    def __init__(
        self,
        *,
        config: BinanceReadonlyReconciliationConfig,
        tracked_symbols: Callable[[], set[str]] | None = None,
        recently_active_symbols: Callable[[], set[str]] | None = None,
        now_ms: Callable[[], int] | None = None,
        http_get_json: Callable[[str, Mapping[str, str], float], Any] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not config.api_key or not config.api_secret:
            raise ReconciliationAuthError("missing_binance_credentials")
        if config.position_epsilon < 0 or config.max_fill_symbols < 1 or config.max_retries < 0:
            raise ValueError("invalid_binance_reconciliation_bounds")
        self._cfg = config
        self._tracked_symbols = tracked_symbols or (lambda: set())
        self._recently_active_symbols = recently_active_symbols or (lambda: set())
        self._now_ms = now_ms or (lambda: int(datetime.now(UTC).timestamp() * 1000))
        self._sleep = sleep or time.sleep
        self._client: http.client.HTTPConnection | None = None
        self._http_get_json = http_get_json or self._default_http_get_json
        self._server_time_offset_ms = 0
        self._request_evidence: list[dict[str, Any]] = []

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def snapshot(self) -> Mapping[str, Any]:
        retrieved_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self._request_evidence = []
        try:
            positions_raw = self._signed_get("/fapi/v3/positionRisk", {})
            all_orders_raw = self._signed_get("/fapi/v1/openOrders", {})
            positions = self._normalize_positions(positions_raw)
            orders = self._normalize_orders(all_orders_raw)
            symbols = self._symbols_for_fills(orders, positions)
            if len(symbols) > self._cfg.max_fill_symbols:
                raise ReconciliationRequestError(
                    endpoint_class="USER_TRADES_SCOPE",
                    environment=self._environment(),
                    reason="max_fill_symbols_exceeded",
                    retry_count=0,
                )
            fills: list[dict[str, Any]] = []
            for symbol in sorted(symbols):
                fills_raw = self._signed_get("/fapi/v1/userTrades", {"symbol": symbol, "startTime": self._now_ms() - self._cfg.trade_lookback_ms})
                fills.extend(self._normalize_fills(fills_raw))
            return {
                "exchange": "binance", "market_type": "USDT_M", "retrieved_at": retrieved_at, "captured_at": retrieved_at,
                "orders": orders, "positions": positions, "fills": fills, "orphan_orders": len(orders),
                "orphan_positions": len([p for p in positions if abs(float(p.get("qty", 0.0) or 0.0)) > self._cfg.position_epsilon]),
                "duplicate_fills": 0, "evidence_status": "COMPLETE", "orphan_coverage": "GLOBAL_OPEN_ORDERS_AND_GLOBAL_POSITION_RISK",
                "fill_symbol_evidence": {"symbols": sorted(symbols), "count": len(symbols), "max": self._cfg.max_fill_symbols, "position_epsilon": self._cfg.position_epsilon},
                "request_evidence": list(self._request_evidence),
            }
        except Exception as exc:  # noqa: BLE001
            diagnostic = self._sanitize_error(exc)
            if diagnostic.get("reason") == "max_fill_symbols_exceeded":
                selected_symbols = locals().get("symbols", set())
                diagnostic.update({"selected_count": len(selected_symbols), "max_fill_symbols": self._cfg.max_fill_symbols, "selected_symbols": sorted(selected_symbols)})
            return {
                "exchange": "binance", "market_type": "USDT_M", "retrieved_at": retrieved_at,
                "orders": [], "positions": [], "fills": [], "orphan_orders": 0, "orphan_positions": 0, "duplicate_fills": 0,
                "evidence_status": "INCOMPLETE", "errors": [diagnostic], "request_evidence": list(self._request_evidence),
            }

    def _signed_get(self, path: str, params: Mapping[str, Any]) -> Any:
        endpoint_class = self._endpoint_class(path)
        symbol = str(params.get("symbol") or "") or None
        timestamp_retry_used = False
        transient_retry_count = 0
        while True:
            # Construct and sign inside the attempt loop: queued/retried requests never inherit an old timestamp.
            timestamp = self._now_ms() + self._server_time_offset_ms
            payload = {**params, "timestamp": timestamp, "recvWindow": self._cfg.recv_window_ms}
            query = urllib.parse.urlencode(payload)
            signature = hmac.new(self._cfg.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            url = f"{self._cfg.base_url.rstrip('/')}{path}?{query}&signature={signature}"
            try:
                result = self._http_get_json(url, {"X-MBX-APIKEY": self._cfg.api_key}, self._cfg.request_timeout_sec)
                self._request_evidence.append({"endpoint_class": endpoint_class, "symbol": symbol, "retry_count": transient_retry_count, "timestamp_refresh": timestamp_retry_used, "environment": self._environment()})
                return result
            except Exception as exc:  # noqa: BLE001
                status, code, timeout_category = self._failure_details(exc)
                if code == -1021 and not timestamp_retry_used:
                    try:
                        self._refresh_server_time_offset()
                    except Exception as refresh_exc:  # noqa: BLE001
                        refresh_status, refresh_code, refresh_timeout = self._failure_details(refresh_exc)
                        raise ReconciliationRequestError(endpoint_class=endpoint_class, symbol=symbol, http_status=refresh_status, binance_code=refresh_code or -1021, retry_count=transient_retry_count, timeout_category=refresh_timeout, environment=self._environment(), reason="server_time_refresh_failed") from refresh_exc
                    timestamp_retry_used = True
                    self._request_evidence.append({"endpoint_class": endpoint_class, "symbol": symbol, "binance_code": -1021, "action": "server_time_offset_refreshed", "environment": self._environment()})
                    continue
                retryable = timeout_category is not None or status == 429 or (status is not None and 500 <= status <= 599)
                if retryable and transient_retry_count < self._cfg.max_retries:
                    transient_retry_count += 1
                    self._sleep(random.uniform(0.025, 0.075) * transient_retry_count)
                    continue
                if status in {401, 403} or code in {-2014, -2015}:
                    raise ReconciliationRequestError(endpoint_class=endpoint_class, symbol=symbol, http_status=status, binance_code=code, retry_count=transient_retry_count, timeout_category=timeout_category, environment=self._environment(), reason="binance_auth_failed") from exc
                raise ReconciliationRequestError(endpoint_class=endpoint_class, symbol=symbol, http_status=status, binance_code=code, retry_count=transient_retry_count, timeout_category=timeout_category, environment=self._environment()) from exc

    def _refresh_server_time_offset(self) -> None:
        url = f"{self._cfg.base_url.rstrip('/')}/fapi/v1/time"
        payload = self._http_get_json(url, {}, self._cfg.request_timeout_sec)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("serverTime"), (int, float)):
            raise ReconciliationPayloadError("server_time_payload_invalid")
        self._server_time_offset_ms = int(payload["serverTime"]) - self._now_ms()

    def _default_http_get_json(self, url: str, headers: Mapping[str, str], timeout_sec: float) -> Any:
        parsed = urllib.parse.urlsplit(url)
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        if self._client is None or self._client.host != parsed.hostname or self._client.port != parsed.port:
            if self._client is not None:
                self._client.close()
            self._client = connection_type(parsed.hostname, parsed.port, timeout=timeout_sec)
        else:
            self._client.timeout = timeout_sec
        target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
        self._client.request("GET", target, headers=dict(headers))
        response = self._client.getresponse()
        body = response.read()
        if response.status >= 400:
            raise error.HTTPError(url, response.status, response.reason, response.headers, io.BytesIO(body))
        return json.loads(body.decode("utf-8"))

    @staticmethod
    def _failure_details(exc: Exception) -> tuple[int | None, int | None, str | None]:
        status: int | None = None
        body: Any = None
        timeout_category: str | None = None
        if isinstance(exc, error.HTTPError):
            status = exc.code
            try:
                body = exc.read().decode("utf-8")
            except Exception:  # noqa: BLE001
                body = None
        if isinstance(exc, (TimeoutError, socket.timeout)):
            timeout_category = "timeout"
        elif isinstance(exc, error.URLError):
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout, ssl.SSLError)) and "timed out" in str(reason).lower():
                timeout_category = "tls_or_connection_timeout"
        code = None
        if body:
            try:
                parsed = json.loads(body)
                code = int(parsed["code"]) if isinstance(parsed, Mapping) and "code" in parsed else None
            except (ValueError, TypeError):
                pass
        return status, code, timeout_category

    @staticmethod
    def _normalize_orders(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list): raise ReconciliationPayloadError("open_orders_payload_not_list")
        out = []
        for row in payload:
            if not isinstance(row, Mapping): raise ReconciliationPayloadError("open_orders_row_not_object")
            out.append({"order_id": str(row.get("orderId") or ""), "symbol": str(row.get("symbol") or ""), "status": str(row.get("status") or "UNKNOWN"), "side": str(row.get("side") or ""), "position_side": str(row.get("positionSide") or "BOTH"), "price": float(row.get("price", 0) or 0), "qty": float(row.get("origQty", 0) or 0), "executed_qty": float(row.get("executedQty", 0) or 0), "created_at": datetime.fromtimestamp(int(row.get("time", 0) or 0) / 1000, tz=UTC).isoformat().replace("+00:00", "Z")})
        return out

    @staticmethod
    def _normalize_positions(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list): raise ReconciliationPayloadError("position_risk_payload_not_list")
        out = []
        for row in payload:
            if not isinstance(row, Mapping): raise ReconciliationPayloadError("position_risk_row_not_object")
            out.append({"symbol": str(row.get("symbol") or ""), "qty": float(row.get("positionAmt", 0) or 0), "entry_price": float(row.get("entryPrice", 0) or 0), "position_side": str(row.get("positionSide") or "BOTH"), "unrealized_pnl": float(row.get("unRealizedProfit", 0) or 0)})
        return out

    @staticmethod
    def _normalize_fills(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list): raise ReconciliationPayloadError("user_trades_payload_not_list")
        out = []
        for row in payload:
            if not isinstance(row, Mapping): raise ReconciliationPayloadError("user_trades_row_not_object")
            out.append({"trade_id": str(row.get("id") or ""), "order_id": str(row.get("orderId") or ""), "symbol": str(row.get("symbol") or ""), "side": "SELL" if bool(row.get("buyer")) is False else "BUY", "position_side": str(row.get("positionSide") or "BOTH"), "qty": float(row.get("qty", 0) or 0), "price": float(row.get("price", 0) or 0), "realized_pnl": float(row.get("realizedPnl", 0) or 0), "time": int(row.get("time", 0) or 0)})
        return out

    def _symbols_for_fills(self, orders: list[dict[str, Any]], positions: list[dict[str, Any]]) -> set[str]:
        symbols = set(self._tracked_symbols()) | set(self._recently_active_symbols())
        symbols.update(str(o.get("symbol") or "") for o in orders if o.get("symbol"))
        symbols.update(str(p.get("symbol") or "") for p in positions if abs(float(p.get("qty", 0) or 0)) > self._cfg.position_epsilon)
        return {symbol.strip().upper() for symbol in symbols if symbol and symbol.strip()}

    def _environment(self) -> str:
        host = urllib.parse.urlparse(self._cfg.base_url).hostname or "unknown"
        return "DEMO" if "demo" in host or "testnet" in host else "PRODUCTION"

    @staticmethod
    def _endpoint_class(path: str) -> str:
        return {"/fapi/v3/positionRisk": "POSITION_RISK", "/fapi/v1/openOrders": "OPEN_ORDERS", "/fapi/v1/userTrades": "USER_TRADES"}.get(path, "UNKNOWN")

    def _sanitize_error(self, exc: Exception) -> dict[str, Any]:
        if isinstance(exc, ReconciliationRequestError):
            return dict(exc.diagnostic)
        return {"reason": str(exc) if isinstance(exc, (ReconciliationAuthError, ReconciliationPayloadError)) else "request_failed_redacted", "endpoint_class": "UNKNOWN", "symbol": None, "http_status": None, "binance_code": None, "retry_count": 0, "timeout_category": None, "environment": self._environment()}
