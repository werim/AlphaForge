from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode, urlparse

import requests
from dotenv import load_dotenv


load_dotenv()

ALLOWED_DEMO_HOSTS = {
    "demo-fapi.binance.com",
    "testnet.binancefuture.com",
    "testnet.binancefutures.com",
}

DEFAULT_BASE_URL = "https://demo-fapi.binance.com"
RECV_WINDOW_MS = 5_000
REQUEST_TIMEOUT_SECONDS = 20
PERCENT_PRICE_RETRY_ATTEMPTS = 5
PERCENT_PRICE_RETRY_DELAY_SECONDS = 3.0
VERIFY_ATTEMPTS = 8
VERIFY_DELAY_SECONDS = 1.0


class BinanceApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def getenv_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return ""


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def floor_to_step(quantity: Decimal, step_size: Decimal) -> Decimal:
    if step_size <= 0:
        return quantity

    units = (quantity / step_size).to_integral_value(rounding=ROUND_DOWN)
    return units * step_size


def split_quantity(
    total_quantity: Decimal,
    *,
    max_quantity: Decimal,
    min_quantity: Decimal,
    step_size: Decimal,
) -> list[Decimal]:
    remaining = abs(total_quantity)
    chunks: list[Decimal] = []

    if remaining <= 0:
        return chunks

    if max_quantity <= 0:
        max_quantity = remaining

    max_chunk = floor_to_step(max_quantity, step_size)
    if max_chunk <= 0:
        raise ValueError(f"Geçersiz max quantity: {max_quantity}")

    while remaining > 0:
        raw_chunk = min(remaining, max_chunk)
        chunk = floor_to_step(raw_chunk, step_size)

        if chunk <= 0:
            break

        if min_quantity > 0 and chunk < min_quantity:
            break

        chunks.append(chunk)
        remaining -= chunk

        if step_size > 0 and remaining < step_size:
            remaining = Decimal("0")

    if remaining > 0:
        raise ValueError(
            "Pozisyon miktarı sembol adımına tam bölünemedi: "
            f"remaining={decimal_text(remaining)}, "
            f"step={decimal_text(step_size)}"
        )

    return chunks


class BinanceDemoFuturesClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_secret = api_secret.encode("utf-8")
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self.time_offset_ms = 0
        self._symbol_rules_cache: dict[str, dict[str, Decimal]] = {}

        parsed = urlparse(self.base_url)
        host = (parsed.hostname or "").lower()

        if parsed.scheme != "https":
            raise ValueError("Base URL HTTPS olmalı.")

        if host not in ALLOWED_DEMO_HOSTS:
            raise ValueError(
                f"Güvenlik engeli: demo olmayan host reddedildi: {host!r}"
            )

    def _decode_response(self, response: requests.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw_text": response.text[:2000]}

        if not response.ok:
            raise BinanceApiError(
                f"Binance HTTP {response.status_code}: {payload}",
                status_code=response.status_code,
                payload=payload,
            )

        if isinstance(payload, dict):
            code = payload.get("code")
            if isinstance(code, int) and code < 0:
                raise BinanceApiError(
                    f"Binance API error: {payload}",
                    status_code=response.status_code,
                    payload=payload,
                )

        return payload

    def public_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = self.session.get(
            self.base_url + path,
            params=params or {},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        return self._decode_response(response)

    def signed_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        signed_params = dict(params or {})
        signed_params["recvWindow"] = RECV_WINDOW_MS
        signed_params["timestamp"] = (
            int(time.time() * 1000) + self.time_offset_ms
        )

        query = urlencode(
            signed_params,
            doseq=True,
            encoding="utf-8",
            safe="",
        )

        signature = hmac.new(
            self.api_secret,
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        url = f"{self.base_url}{path}?{query}&signature={signature}"

        response = self.session.request(
            method=method,
            url=url,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        return self._decode_response(response)

    def sync_time(self) -> None:
        started_ms = int(time.time() * 1000)
        payload = self.public_get("/fapi/v1/time")
        completed_ms = int(time.time() * 1000)

        server_ms = int(payload["serverTime"])
        estimated_local_ms = (started_ms + completed_ms) // 2
        self.time_offset_ms = server_ms - estimated_local_ms

        print(
            f"[TIME] server offset: {self.time_offset_ms:+d} ms",
            flush=True,
        )

    def get_exchange_info(self) -> dict[str, Any]:
        payload = self.public_get("/fapi/v1/exchangeInfo")

        if not isinstance(payload, dict):
            raise BinanceApiError(
                "exchangeInfo response object değil",
                payload=payload,
            )

        return payload

    def get_symbol_rules(self, symbol: str) -> dict[str, Decimal]:
        cached = self._symbol_rules_cache.get(symbol)
        if cached is not None:
            return cached

        payload = self.get_exchange_info()
        symbols = payload.get("symbols")

        if not isinstance(symbols, list):
            raise BinanceApiError(
                "exchangeInfo symbols listesi bulunamadı",
                payload=payload,
            )

        symbol_row = next(
            (
                row
                for row in symbols
                if isinstance(row, dict) and row.get("symbol") == symbol
            ),
            None,
        )

        if symbol_row is None:
            raise BinanceApiError(
                f"exchangeInfo içinde sembol bulunamadı: {symbol}"
            )

        filters = {
            row.get("filterType"): row
            for row in symbol_row.get("filters", [])
            if isinstance(row, dict)
        }

        selected = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE")
        if not selected:
            raise BinanceApiError(
                f"{symbol}: MARKET_LOT_SIZE/LOT_SIZE bulunamadı"
            )

        rules = {
            "min_qty": Decimal(str(selected.get("minQty", "0"))),
            "max_qty": Decimal(str(selected.get("maxQty", "0"))),
            "step_size": Decimal(str(selected.get("stepSize", "0"))),
        }
        self._symbol_rules_cache[symbol] = rules
        return rules

    def get_open_orders(self) -> list[dict[str, Any]]:
        payload = self.signed_request("GET", "/fapi/v1/openOrders")
        if not isinstance(payload, list):
            raise BinanceApiError(
                "openOrders response list değil",
                payload=payload,
            )
        return payload

    def get_positions(self) -> list[dict[str, Any]]:
        payload = self.signed_request("GET", "/fapi/v3/positionRisk")
        if not isinstance(payload, list):
            raise BinanceApiError(
                "positionRisk response list değil",
                payload=payload,
            )
        return payload

    def cancel_all_orders_for_symbol(self, symbol: str) -> Any:
        return self.signed_request(
            "DELETE",
            "/fapi/v1/allOpenOrders",
            {"symbol": symbol},
        )

    def close_position(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        position_side: str,
    ) -> Any:
        if quantity == 0:
            raise ValueError("Sıfır miktarlı pozisyon kapatılamaz.")

        side = "SELL" if quantity > 0 else "BUY"

        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": decimal_text(abs(quantity)),
            "newOrderRespType": "RESULT",
        }

        normalized_position_side = position_side.upper()

        if normalized_position_side in {"LONG", "SHORT"}:
            params["positionSide"] = normalized_position_side
        else:
            params["positionSide"] = "BOTH"
            params["reduceOnly"] = "true"

        return self.signed_request(
            "POST",
            "/fapi/v1/order",
            params,
        )


def active_positions(
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for row in positions:
        try:
            quantity = Decimal(str(row.get("positionAmt", "0")))
        except (InvalidOperation, TypeError, ValueError):
            raise BinanceApiError(
                f"Geçersiz positionAmt: {row!r}"
            ) from None

        if quantity != 0:
            result.append(
                {
                    **row,
                    "_quantity_decimal": quantity,
                }
            )

    return result


def print_state(
    orders: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> None:
    active = active_positions(positions)

    print()
    print("=== CURRENT DEMO FUTURES STATE ===")
    print(f"Open orders     : {len(orders)}")
    print(f"Active positions: {len(active)}")

    if orders:
        print()
        print("Open order symbols:")
        for symbol in sorted(
            {str(row.get("symbol") or "") for row in orders}
        ):
            print(f"  - {symbol}")

    if active:
        print()
        print("Active positions:")
        for row in active:
            print(
                "  - "
                f"{row.get('symbol')} "
                f"positionSide={row.get('positionSide', 'BOTH')} "
                f"qty={decimal_text(row['_quantity_decimal'])}"
            )


def cancel_all_open_orders(
    client: BinanceDemoFuturesClient,
    orders: list[dict[str, Any]],
    *,
    execute: bool,
) -> None:
    symbols = sorted(
        {
            str(row.get("symbol") or "")
            for row in orders
            if str(row.get("symbol") or "")
        }
    )

    if not symbols:
        print("[ORDERS] Açık emir yok.")
        return

    print(f"[ORDERS] {len(symbols)} sembolde açık emir bulundu.")

    for index, symbol in enumerate(symbols, start=1):
        if not execute:
            print(
                f"[DRY-RUN] [{index}/{len(symbols)}] "
                f"{symbol}: tüm açık emirler iptal edilecek"
            )
            continue

        print(
            f"[CANCEL] [{index}/{len(symbols)}] {symbol}",
            flush=True,
        )

        result = client.cancel_all_orders_for_symbol(symbol)

        print(
            "[CANCELLED] "
            f"{symbol}: {json.dumps(result, ensure_ascii=False)}"
        )

        time.sleep(0.15)


def close_all_positions(
    client: BinanceDemoFuturesClient,
    positions: list[dict[str, Any]],
    *,
    execute: bool,
) -> None:
    active = active_positions(positions)

    if not active:
        print("[POSITIONS] Aktif pozisyon yok.")
        return

    print(f"[POSITIONS] {len(active)} aktif pozisyon bulundu.")

    for position_index, row in enumerate(active, start=1):
        symbol = str(row.get("symbol") or "")
        position_side = str(
            row.get("positionSide") or "BOTH"
        ).upper()
        quantity: Decimal = row["_quantity_decimal"]

        if not symbol:
            print(
                f"[ERROR] Boş sembol: {row!r}",
                file=sys.stderr,
            )
            continue

        try:
            rules = client.get_symbol_rules(symbol)
            chunks = split_quantity(
                quantity,
                max_quantity=rules["max_qty"],
                min_quantity=rules["min_qty"],
                step_size=rules["step_size"],
            )
        except Exception as exc:
            print(
                f"[ERROR] {symbol}: miktar kuralları alınamadı: "
                f"{type(exc).__name__}:{exc}",
                file=sys.stderr,
            )
            continue

        print(
            f"[POSITION {position_index}/{len(active)}] "
            f"{symbol} qty={decimal_text(quantity)} "
            f"maxQty={decimal_text(rules['max_qty'])} "
            f"stepSize={decimal_text(rules['step_size'])} "
            f"chunks={len(chunks)}"
        )

        close_side = "SELL" if quantity > 0 else "BUY"
        chunk_failed = False

        for chunk_index, chunk in enumerate(chunks, start=1):
            signed_chunk = chunk if quantity > 0 else -chunk

            if not execute:
                print(
                    f"[DRY-RUN] {symbol} "
                    f"chunk={chunk_index}/{len(chunks)} "
                    f"{close_side} MARKET "
                    f"qty={decimal_text(chunk)}"
                )
                continue

            print(
                f"[CLOSE] {symbol} "
                f"chunk={chunk_index}/{len(chunks)} "
                f"qty={decimal_text(chunk)}",
                flush=True,
            )

            success = False

            for attempt in range(1, PERCENT_PRICE_RETRY_ATTEMPTS + 1):
                try:
                    result = client.close_position(
                        symbol=symbol,
                        quantity=signed_chunk,
                        position_side=position_side,
                    )

                    print(
                        f"[CLOSED] {symbol} "
                        f"chunk={chunk_index}/{len(chunks)} "
                        f"orderId="
                        f"{result.get('orderId') if isinstance(result, dict) else None} "
                        f"status="
                        f"{result.get('status') if isinstance(result, dict) else None}"
                    )

                    success = True
                    break

                except BinanceApiError as exc:
                    payload = (
                        exc.payload
                        if isinstance(exc.payload, dict)
                        else {}
                    )
                    code = payload.get("code")

                    if code == -4131 and attempt < PERCENT_PRICE_RETRY_ATTEMPTS:
                        print(
                            f"[RETRY] {symbol}: PERCENT_PRICE filter, "
                            f"attempt={attempt}/{PERCENT_PRICE_RETRY_ATTEMPTS}. "
                            "Order book düzelmesi bekleniyor.",
                            file=sys.stderr,
                        )
                        time.sleep(PERCENT_PRICE_RETRY_DELAY_SECONDS)
                        continue

                    print(
                        f"[ERROR] {symbol} "
                        f"chunk={chunk_index}/{len(chunks)}: {exc}",
                        file=sys.stderr,
                    )
                    break

            if not success:
                print(
                    f"[SAFETY] {symbol}: kalan parçalar gönderilmedi. "
                    "Pozisyon yeniden sorgulanacak.",
                    file=sys.stderr,
                )
                chunk_failed = True
                break

            time.sleep(0.35)

        if not chunk_failed and execute:
            print(
                f"[POSITION COMPLETE] {symbol}",
                flush=True,
            )


def verify_empty(
    client: BinanceDemoFuturesClient,
    *,
    attempts: int = VERIFY_ATTEMPTS,
    delay_seconds: float = VERIFY_DELAY_SECONDS,
) -> bool:
    for attempt in range(1, attempts + 1):
        orders = client.get_open_orders()
        positions = client.get_positions()
        active = active_positions(positions)

        print(
            f"[VERIFY {attempt}/{attempts}] "
            f"open_orders={len(orders)} "
            f"active_positions={len(active)}"
        )

        if not orders and not active:
            print()
            print("SUCCESS: Demo Futures hesabı tamamen temiz.")
            return True

        if attempt < attempts:
            time.sleep(delay_seconds)

    print()
    print("FAILED: Hesap tamamen temizlenemedi.", file=sys.stderr)

    orders = client.get_open_orders()
    positions = client.get_positions()
    print_state(orders, positions)

    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Binance Demo USD-M Futures hesabındaki tüm açık emirleri "
            "iptal eder ve tüm pozisyonları kapatır."
        )
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help="Gerçek Demo iptal ve kapatma emirlerini gönder.",
    )

    parser.add_argument(
        "--base-url",
        default=getenv_first(
            "BINANCE_BASE_URL",
            "BINANCE_FUTURES_BASE_URL",
            "ALPHAFORGE_BINANCE_BASE_URL",
        )
        or DEFAULT_BASE_URL,
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    api_key = getenv_first(
        "BINANCE_API_KEY",
        "ALPHAFORGE_BINANCE_API_KEY",
    )
    api_secret = getenv_first(
        "BINANCE_API_SECRET",
        "ALPHAFORGE_BINANCE_API_SECRET",
    )

    if not api_key or not api_secret:
        print(
            "BINANCE_API_KEY ve BINANCE_API_SECRET bulunamadı.",
            file=sys.stderr,
        )
        return 2

    if args.execute:
        confirmation = os.getenv("BINANCE_DEMO_CLEAN_CONFIRM", "")
        if confirmation != "YES":
            print(
                "Gerçek çalıştırma engellendi. Önce şunu ayarla:\n"
                '$env:BINANCE_DEMO_CLEAN_CONFIRM="YES"',
                file=sys.stderr,
            )
            return 2

    client = BinanceDemoFuturesClient(
        base_url=args.base_url,
        api_key=api_key,
        api_secret=api_secret,
    )

    print(f"Base URL: {client.base_url}")
    print(f"Mode    : {'EXECUTE' if args.execute else 'DRY-RUN'}")

    client.sync_time()

    initial_orders = client.get_open_orders()
    initial_positions = client.get_positions()

    print_state(initial_orders, initial_positions)

    cancel_all_open_orders(
        client,
        initial_orders,
        execute=args.execute,
    )

    if args.execute:
        time.sleep(1.0)

    refreshed_positions = client.get_positions()

    close_all_positions(
        client,
        refreshed_positions,
        execute=args.execute,
    )

    if not args.execute:
        print()
        print(
            "DRY-RUN tamamlandı. Hiçbir emir iptal edilmedi "
            "ve hiçbir pozisyon kapatılmadı."
        )
        return 0

    return 0 if verify_empty(client) else 1


if __name__ == "__main__":
    raise SystemExit(main())