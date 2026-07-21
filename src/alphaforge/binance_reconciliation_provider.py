from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import http.client
import io
import json
import random
import re
import socket
import ssl
import time
import urllib.parse
from urllib import error
from typing import Any, Callable, Mapping

from alphaforge.env_contract import resolve_binance_environment


class ReconciliationAuthError(RuntimeError):
    pass


class ReconciliationPayloadError(RuntimeError):
    pass


class ReconciliationScopeError(RuntimeError):
    pass


class ReconciliationExposureError(ReconciliationPayloadError):
    def __init__(self, message: str, positions: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.positions = positions


@dataclass(frozen=True, slots=True)
class BinanceReadonlyReconciliationConfig:
    base_url: str
    api_key: str
    api_secret: str
    recv_window_ms: int = 5000
    request_timeout_sec: float = 2.0
    trade_lookback_ms: int = 3_600_000
    position_epsilon: Decimal = Decimal("0.00000001")
    max_fill_symbols: int = 10
    transport_retries: int = 1


class BinanceHttpTransport:
    """Small serial keep-alive transport; failed connections are never reused."""

    def __init__(self) -> None:
        self._connection: http.client.HTTPConnection | None = None
        self._origin: tuple[str, str, int] | None = None

    def close(self) -> None:
        connection, self._connection = self._connection, None
        self._origin = None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def get_json(self, url: str, headers: Mapping[str, str], timeout_sec: float) -> Any:
        parsed = urllib.parse.urlsplit(url)
        origin = (parsed.scheme, parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80))
        if self._connection is None or self._origin != origin:
            self.close()
            cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
            self._connection = cls(origin[1], origin[2], timeout=timeout_sec)
            self._origin = origin
        path = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
        try:
            self._connection.request("GET", path, headers=dict(headers))
            response = self._connection.getresponse()
            body = response.read()
            if response.status >= 400:
                raise error.HTTPError(url, response.status, response.reason, response.headers, io.BytesIO(body))
            return json.loads(body.decode("utf-8"))
        except Exception:
            self.close()
            raise


_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}$")
_OPEN_STATUSES = {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}


def normalize_reconciliation_symbol(raw: Any, source: str = "tracked") -> str:
    symbol = str(raw or "").strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise ReconciliationScopeError(f"invalid_symbol:{source}")
    return symbol


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
    def __init__(self, *, config: BinanceReadonlyReconciliationConfig,
                 tracked_symbols: Callable[[], set[str]] | None = None,
                 now_ms: Callable[[], int] | None = None,
                 http_get_json: Callable[[str, Mapping[str, str], float], Any] | None = None,
                 transport: BinanceHttpTransport | None = None) -> None:
        if not config.api_key or not config.api_secret:
            raise ReconciliationAuthError("missing_binance_credentials")
        self._cfg = config
        self._tracked_symbols = tracked_symbols or (lambda: set())
        self._now_ms = now_ms or (lambda: int(datetime.now(UTC).timestamp() * 1000))
        self._transport = transport or BinanceHttpTransport()
        self._http_get_json = http_get_json or self._transport.get_json
        self._time_offset_ms = 0
        self._last_timestamp_ms = 0
        self._request_evidence: list[dict[str, Any]] = []
        self._request_attempts: list[dict[str, Any]] = []
        self._http_request_count = 0

    def close(self) -> None:
        self._transport.close()

    def snapshot(self) -> Mapping[str, Any]:
        retrieved_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        positions: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        fills: list[dict[str, Any]] = []
        coverage = {"positionRisk": False, "openOrders": False, "userTrades": []}
        self._request_evidence = []
        self._request_attempts = []
        self._http_request_count = 0
        selected: list[str] = []
        sources: dict[str, list[str]] = {}
        failed_endpoint: str | None = None
        failed_symbol: str | None = None
        position_warnings: list[dict[str, Any]] = []
        try:
            failed_endpoint = "positionRisk"
            raw_positions = self._signed_get("/fapi/v3/positionRisk", {}, "positionRisk")
            positions, position_warnings = self._normalize_positions(raw_positions)
            coverage["positionRisk"] = True
            failed_endpoint = "openOrders"
            orders = self._normalize_orders(self._signed_get("/fapi/v1/openOrders", {}, "openOrders"))
            coverage["openOrders"] = True
            selected, sources = self._symbols_for_fills(orders, positions)
            if len(selected) > self._cfg.max_fill_symbols:
                raise ReconciliationScopeError("fill_symbol_scope_exceeds_configured_max")
            failed_endpoint = "userTrades"
            for symbol in selected:
                failed_symbol = symbol
                raw = self._signed_get("/fapi/v1/userTrades", {"symbol": symbol, "startTime": self._now_ms() - self._cfg.trade_lookback_ms}, "userTrades", symbol)
                fills.extend(self._normalize_fills(raw))
                coverage["userTrades"].append(symbol)
            active = sum(1 for p in positions if p["active"])
            return self._snapshot_base(retrieved_at, positions, orders, fills, coverage, selected, sources, position_warnings) | {
                "orphan_orders": len(orders), "orphan_positions": active, "duplicate_fills": 0,
                "evidence_status": "COMPLETE", "errors": [], "failed_endpoint": None,
                "failed_symbol": None, "unknown_unreconciled_symbols": [],
            }
        except Exception as exc:  # fail closed while preserving completed evidence
            if isinstance(exc, ReconciliationExposureError):
                positions = exc.positions
            unknown = [s for s in selected if s not in coverage["userTrades"]]
            return self._snapshot_base(retrieved_at, positions, orders, fills, coverage, selected, sources, position_warnings) | {
                "orphan_orders": None, "orphan_positions": None, "duplicate_fills": None,
                "evidence_status": "INCOMPLETE", "errors": [self._sanitize_error(exc)],
                "failed_endpoint": failed_endpoint, "failed_symbol": failed_symbol,
                "unknown_unreconciled_symbols": unknown,
            }
        finally:
            # A snapshot boundary cannot retain possibly stale/poisoned state.
            self.close()

    def _snapshot_base(self, at: str, positions: list[dict[str, Any]], orders: list[dict[str, Any]],
                       fills: list[dict[str, Any]], coverage: dict[str, Any], selected: list[str],
                       sources: dict[str, list[str]], position_warnings: list[dict[str, Any]]) -> dict[str, Any]:
        return {"exchange": "binance", "market_type": "USDT_M", "retrieved_at": at, "captured_at": at,
                "orders": orders, "positions": positions, "fills": fills, "coverage": coverage,
                "selected_count": len(selected), "configured_max": self._cfg.max_fill_symbols,
                "selected_symbols": selected, "symbol_sources": sources,
                "position_warnings": position_warnings,
                "http_request_count": self._http_request_count, "request_count": self._http_request_count,
                "request_attempts": list(self._request_attempts),
                "endpoint_results": list(self._request_evidence), "request_evidence": list(self._request_evidence),
                "orphan_coverage": "GLOBAL_OPEN_ORDERS_AND_GLOBAL_POSITION_RISK"}

    def _signed_get(self, path: str, params: Mapping[str, Any], endpoint: str, symbol: str | None = None) -> Any:
        time_refreshed = False
        transient_retries = 0
        timestamp_retry = 0
        while True:
            retry_attempt: dict[str, Any] | None = None
            timestamp = max(self._now_ms() + self._time_offset_ms, self._last_timestamp_ms + 1)
            self._last_timestamp_ms = timestamp  # unique and generated immediately before signing
            payload = {**params, "timestamp": timestamp, "recvWindow": self._cfg.recv_window_ms}
            query = urllib.parse.urlencode(payload)
            signature = hmac.new(self._cfg.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            url = f"{self._cfg.base_url.rstrip('/')}{path}?{query}&signature={signature}"
            code: int | None = None
            try:
                result, attempt = self._http_operation(url, {"X-MBX-APIKEY": self._cfg.api_key}, endpoint, symbol,
                                                       "SIGNED", transient_retries + timestamp_retry + 1, None)
                if isinstance(result, Mapping) and isinstance(result.get("code"), int) and int(result["code"]) < 0:
                    code = int(result["code"])
                    if code == -1021 and timestamp_retry == 0:
                        attempt.update({"outcome": "RETRY", "retry_reason": "BINANCE_-1021", "binance_code": code,
                                        "time_refresh_performed": True})
                        self._refresh_server_time(); time_refreshed = True; timestamp_retry = 1
                        continue
                    attempt.update({"outcome": "FAIL", "binance_code": code})
                    raise ReconciliationAuthError(f"binance_error_code_{code}")
                self._record(endpoint, symbol, code, time_refreshed, transient_retries + timestamp_retry, "PASS")
                return result
            except error.HTTPError as exc:
                retry_attempt = getattr(exc, "_alphaforge_attempt", None)
                http_status = exc.code
                binance_code = None
                binance_message = None
                try:
                    body = json.loads(exc.read().decode("utf-8"))
                    binance_code = int(body.get("code")) if isinstance(body, Mapping) else None
                    binance_message = self._safe_binance_message(body.get("msg")) if isinstance(body, Mapping) else None
                except Exception:
                    binance_code = getattr(exc, "_alphaforge_binance_code", None)
                    binance_message = getattr(exc, "_alphaforge_binance_message", None)
                if binance_code == -1021 and timestamp_retry == 0:
                    attempt = getattr(exc, "_alphaforge_attempt", None)
                    if attempt is not None:
                        attempt.update({"outcome": "RETRY", "retry_reason": "BINANCE_-1021", "binance_code": binance_code,
                                        "time_refresh_performed": True})
                    self.close(); self._refresh_server_time(); time_refreshed = True; timestamp_retry = 1
                    continue
                transient = http_status == 429 or 500 <= http_status < 600
                if http_status in {401, 403}:
                    self._record(endpoint, symbol, binance_code, time_refreshed, transient_retries + timestamp_retry, "FAIL", http_status, binance_message)
                    raise ReconciliationAuthError(f"binance_auth_failed_status_{http_status}") from exc
                if not transient or transient_retries >= self._cfg.transport_retries:
                    self._record(endpoint, symbol, binance_code, time_refreshed, transient_retries + timestamp_retry, "FAIL", http_status, binance_message)
                    raise ReconciliationPayloadError(f"binance_http_error:status={http_status}:code={binance_code}") from exc
            except (TimeoutError, OSError, ssl.SSLError, http.client.HTTPException, error.URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                retry_attempt = getattr(exc, "_alphaforge_attempt", None)
                if transient_retries >= self._cfg.transport_retries:
                    self._record(endpoint, symbol, code, time_refreshed, transient_retries + timestamp_retry, "FAIL"); raise
            self.close()
            if retry_attempt is not None:
                retry_attempt.update({"outcome": "RETRY", "retry_reason": "TRANSIENT_FAILURE"})
            transient_retries += 1
            time.sleep(random.uniform(0.01, 0.05) * transient_retries)

    def _refresh_server_time(self) -> None:
        url = f"{self._cfg.base_url.rstrip('/')}/fapi/v1/time"
        payload, _ = self._http_operation(url, {}, "serverTime", None, "SERVER_TIME", 1, "BINANCE_-1021")
        if not isinstance(payload, Mapping) or not isinstance(payload.get("serverTime"), int):
            raise ReconciliationPayloadError("server_time_payload_invalid")
        self._time_offset_ms = int(payload["serverTime"]) - self._now_ms()

    def _http_operation(self, url: str, headers: Mapping[str, str], endpoint: str, symbol: str | None,
                        request_kind: str, attempt_number: int, retry_reason: str | None) -> tuple[Any, dict[str, Any]]:
        self._http_request_count += 1
        attempt = {"sequence": self._http_request_count, "endpoint_class": endpoint, "symbol": symbol,
                   "request_kind": request_kind, "attempt_number": attempt_number, "retry_reason": retry_reason,
                   "http_status": None, "binance_code": None, "transport_category": None, "outcome": "FAIL",
                   "time_refresh_performed": request_kind == "SERVER_TIME",
                   "environment": urllib.parse.urlsplit(self._cfg.base_url).hostname}
        self._request_attempts.append(attempt)
        try:
            payload = self._http_get_json(url, headers, self._cfg.request_timeout_sec)
            attempt["outcome"] = "PASS"
            return payload, attempt
        except error.HTTPError as exc:
            attempt["http_status"] = exc.code
            attempt["transport_category"] = "HTTP"
            try:
                body_bytes = exc.read()
                body = json.loads(body_bytes.decode("utf-8"))
                attempt["binance_code"] = int(body.get("code")) if isinstance(body, Mapping) and body.get("code") is not None else None
                exc.fp = io.BytesIO(body_bytes)
                exc.file = exc.fp
                setattr(exc, "_alphaforge_binance_code", attempt["binance_code"])
                setattr(exc, "_alphaforge_binance_message", self._safe_binance_message(body.get("msg")) if isinstance(body, Mapping) else None)
            except Exception:
                pass
            setattr(exc, "_alphaforge_attempt", attempt)
            raise
        except Exception as exc:
            attempt["transport_category"] = self._transport_category(exc)
            setattr(exc, "_alphaforge_attempt", attempt)
            raise

    @staticmethod
    def _transport_category(exc: Exception) -> str:
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "TIMEOUT"
        if isinstance(exc, ssl.SSLError):
            return "TLS"
        if isinstance(exc, error.URLError):
            return "URL"
        if isinstance(exc, http.client.HTTPException):
            return "HTTP_PROTOCOL"
        return "TRANSPORT"

    def _record(self, endpoint: str, symbol: str | None, code: int | None, refreshed: bool, retries: int, outcome: str,
                http_status: int | None = None, message: str | None = None) -> None:
        self._request_evidence.append({"endpoint_class": endpoint, "symbol": symbol, "binance_code": code,
                                       "http_status": http_status, "binance_message": message,
                                       "time_refresh_performed": refreshed, "retry_count": retries, "final_outcome": outcome})

    def _normalize_positions(self, payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not isinstance(payload, list): raise ReconciliationPayloadError("position_risk_payload_not_list")
        out = []
        warnings: list[dict[str, Any]] = []
        for row in payload:
            if not isinstance(row, Mapping): raise ReconciliationPayloadError("position_risk_row_not_object")
            raw_qty = row.get("positionAmt")
            try:
                if raw_qty is None: raise InvalidOperation
                qty = Decimal(str(raw_qty))
                if not qty.is_finite(): raise InvalidOperation
            except (InvalidOperation, ValueError, TypeError):
                raise ReconciliationExposureError("malformed_position_amount", out) from None
            raw_symbol = row.get("symbol")
            normalized = str(raw_symbol or "").strip().upper()
            valid = bool(_SYMBOL.fullmatch(normalized))
            exact_zero = qty == 0
            epsilon_filtered = not exact_zero and abs(qty) <= self._cfg.position_epsilon
            active = abs(qty) > self._cfg.position_epsilon
            symbol = normalized if valid else self._sanitized_symbol(raw_symbol)
            normalized_row = {"symbol": symbol, "symbol_valid": valid, "qty": float(qty), "qty_exact": str(qty),
                        "active": active, "epsilon_filtered": epsilon_filtered, "exact_zero": exact_zero,
                        "entry_price": float(row.get("entryPrice", 0) or 0), "position_side": str(row.get("positionSide") or "BOTH"),
                        "unrealized_pnl": float(row.get("unRealizedProfit", 0) or 0)}
            out.append(normalized_row)
            if not valid and exact_zero:
                warnings.append({"category": "zero_exposure_invalid_symbol", "symbol": symbol})
            elif not valid:
                classification = "active_position_invalid_symbol" if active else "epsilon_position_invalid_symbol"
                raise ReconciliationExposureError(classification, out)
        return out, warnings

    def _normalize_orders(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list): raise ReconciliationPayloadError("open_orders_payload_not_list")
        out = []
        for row in payload:
            if not isinstance(row, Mapping): raise ReconciliationPayloadError("open_orders_row_not_object")
            status = str(row.get("status") or "UNKNOWN").upper()
            if status not in _OPEN_STATUSES: continue
            symbol = self._valid_symbol(row.get("symbol"), "order")
            out.append({"order_id": str(row.get("orderId") or ""), "symbol": symbol, "status": status,
                        "side": str(row.get("side") or ""), "position_side": str(row.get("positionSide") or "BOTH"),
                        "price": float(row.get("price", 0) or 0), "qty": float(row.get("origQty", 0) or 0),
                        "executed_qty": float(row.get("executedQty", 0) or 0),
                        "created_at": datetime.fromtimestamp(int(row.get("time", 0) or 0) / 1000, tz=UTC).isoformat().replace("+00:00", "Z")})
        return out

    @staticmethod
    def _normalize_fills(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list): raise ReconciliationPayloadError("user_trades_payload_not_list")
        if any(not isinstance(row, Mapping) for row in payload): raise ReconciliationPayloadError("user_trades_row_not_object")
        return [{"trade_id": str(r.get("id") or ""), "order_id": str(r.get("orderId") or ""),
                 "symbol": str(r.get("symbol") or "").upper(), "side": "SELL" if r.get("buyer") is False else "BUY",
                 "position_side": str(r.get("positionSide") or "BOTH"), "qty": float(r.get("qty", 0) or 0),
                 "price": float(r.get("price", 0) or 0), "realized_pnl": float(r.get("realizedPnl", 0) or 0),
                 "time": int(r.get("time", 0) or 0)} for r in payload if isinstance(r, Mapping)]

    def _symbols_for_fills(self, orders: list[dict[str, Any]], positions: list[dict[str, Any]]) -> tuple[list[str], dict[str, list[str]]]:
        sources: dict[str, set[str]] = {}
        for raw in self._tracked_symbols(): sources.setdefault(self._valid_symbol(raw, "tracked"), set()).add("tracked")
        for row in orders: sources.setdefault(row["symbol"], set()).add("open_order")
        for row in positions:
            if row["active"]: sources.setdefault(row["symbol"], set()).add("active_position")
        selected = sorted(sources)
        return selected, {s: sorted(sources[s]) for s in selected}

    @staticmethod
    def _valid_symbol(raw: Any, source: str) -> str:
        return normalize_reconciliation_symbol(raw, source)

    @staticmethod
    def _sanitized_symbol(raw: Any) -> str:
        digest = hashlib.sha256(str(raw or "").encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"INVALID_SYMBOL_SHA256_{digest}"

    @staticmethod
    def _safe_binance_message(raw: Any) -> str | None:
        if raw is None:
            return None
        message = str(raw).replace("\r", " ").replace("\n", " ")[:200]
        message = re.sub(r"(?i)(signature|api[-_ ]?key|secret)\s*[=:]\s*[^ &]+", r"\1=REDACTED", message)
        return message

    @staticmethod
    def _sanitize_error(exc: Exception) -> str:
        safe = str(exc) if isinstance(exc, (ReconciliationPayloadError, ReconciliationScopeError, ReconciliationAuthError)) else "request_failed_redacted"
        return f"{exc.__class__.__name__}:{safe}"
