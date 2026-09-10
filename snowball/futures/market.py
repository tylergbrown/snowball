"""Coinbase perpetual futures marks + dual-gated INTX swap orders for Future Trader."""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

from snowball.models import Ticker

log = logging.getLogger("snowball.futures.market")

# Coinbase Advanced Trade product_id → ccxt unified symbol
PRODUCT_TO_CCXT: dict[str, str] = {
    "SPY-PERP-INTX": "SPY/USDC:USDC",
    "QQQ-PERP-INTX": "QQQ/USDC:USDC",
}

DEFAULT_FUTURES_PRODUCTS: tuple[str, ...] = ("SPY-PERP-INTX", "QQQ-PERP-INTX")

_STABLES = ("USD", "USDC", "USDT")


def _expand_env_newlines(value: str) -> str:
    """Turn .env-style \\n escapes into real newlines (CDP PEM). Local copy — no live import."""
    return (value or "").replace("\\n", "\n").replace("\\r", "\r")


def normalize_futures_product(product: str) -> str:
    """Normalize to Coinbase product id (e.g. SPY-PERP-INTX)."""
    p = product.strip().upper().replace("_", "-")
    if p in PRODUCT_TO_CCXT:
        return p
    for pid, unified in PRODUCT_TO_CCXT.items():
        if p == unified.upper() or p.replace("/", "-") == unified.upper().replace("/", "-"):
            return pid
        base = unified.split("/")[0].upper()
        if p in (base, f"{base}-PERP", f"{base}-PERP-INTX"):
            return pid
    return p


def to_futures_ccxt_symbol(product: str) -> str:
    pid = normalize_futures_product(product)
    if pid in PRODUCT_TO_CCXT:
        return PRODUCT_TO_CCXT[pid]
    if "/" in pid:
        return pid
    raise ValueError(f"unknown futures product {product!r}; known: {sorted(PRODUCT_TO_CCXT)}")


def parse_futures_order_fill(order: dict[str, Any]) -> tuple[float, float, float]:
    """Return (fill_px, fill_qty, fee_usd) from a ccxt order response. Local copy (no live import)."""
    filled = float(order.get("filled") or 0.0)
    average = order.get("average")
    price = order.get("price")
    cost = order.get("cost")
    if average is not None and float(average) > 0:
        fill_px = float(average)
    elif price is not None and float(price) > 0:
        fill_px = float(price)
    elif cost is not None and filled > 0:
        fill_px = float(cost) / filled
    else:
        fill_px = 0.0
    fee_usd = 0.0
    fee = order.get("fee")
    if isinstance(fee, dict) and fee.get("cost") is not None:
        currency = str(fee.get("currency") or "").upper()
        if currency in ("", "USD", "USDT", "USDC"):
            fee_usd = float(fee["cost"])
        elif fill_px > 0:
            fee_usd = float(fee["cost"]) * fill_px
        else:
            fee_usd = float(fee["cost"])
    elif isinstance(order.get("fees"), list):
        for part in order["fees"]:
            if not isinstance(part, dict) or part.get("cost") is None:
                continue
            currency = str(part.get("currency") or "").upper()
            cost_f = float(part["cost"])
            if currency in ("", "USD", "USDT", "USDC"):
                fee_usd += cost_f
            elif fill_px > 0:
                fee_usd += cost_f * fill_px
            else:
                fee_usd += cost_f
    if filled <= 0 and cost is not None and fill_px > 0:
        filled = float(cost) / fill_px
    return fill_px, filled, fee_usd


def estimate_account_value_usd(
    balance: dict[str, Any] | None,
    *,
    crypto_marks: dict[str, float] | None = None,
) -> float:
    """Estimate total Coinbase account value in USD.

    Prefer free+used for USD/USDC/USDT. Add other `total` balances marked with
    crypto_marks (product → USD price) when provided. Falls back to summing
    numeric totals for stables only when free/used missing.
    """
    if not isinstance(balance, dict):
        return 0.0
    free = balance.get("free") if isinstance(balance.get("free"), dict) else {}
    used = balance.get("used") if isinstance(balance.get("used"), dict) else {}
    totals = balance.get("total") if isinstance(balance.get("total"), dict) else {}
    value = 0.0
    seen: set[str] = set()
    for ccy in _STABLES:
        f = float(free.get(ccy) or 0.0)
        u = float(used.get(ccy) or 0.0)
        if f or u:
            value += f + u
            seen.add(ccy)
        elif totals.get(ccy) is not None:
            value += float(totals[ccy] or 0.0)
            seen.add(ccy)
    # Optional crypto MTM via marks keyed by Coinbase product (e.g. BTC-USD)
    if crypto_marks and totals:
        for ccy, amt in totals.items():
            if ccy in seen or ccy in _STABLES:
                continue
            try:
                qty = float(amt or 0.0)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            px = None
            for product, mark in crypto_marks.items():
                base = product.split("-")[0].upper()
                if base == str(ccy).upper():
                    px = float(mark)
                    break
            if px is not None and px > 0:
                value += qty * px
    # Coinbase sometimes nests portfolio totals under info
    info = balance.get("info")
    if isinstance(info, dict) and value <= 0:
        for key in ("total_balance", "portfolio_value", "equity", "balance"):
            raw = info.get(key)
            try:
                if raw is not None and float(raw) > 0:
                    return float(raw)
            except (TypeError, ValueError):
                continue
    return max(0.0, value)


def round_amount_down(amount: float, precision: float = 0.01) -> float:
    if precision <= 0:
        return amount
    steps = math.floor(amount / precision + 1e-12)
    return max(0.0, steps * precision)


class CoinbaseFuturesMarket:
    """Coinbase perp OHLCV/tickers; live swap orders only when caller dual-gates."""

    mark_source = "coinbase_perp"

    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        exchange: object | None = None,
        timeout: float = 20000,
        allow_orders: bool = False,
    ) -> None:
        self._timeout = timeout
        self._allow_orders = bool(allow_orders)
        if exchange is not None:
            self._exchange = exchange
            return
        import ccxt  # lazy so unit tests can inject a fake

        opts: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": int(timeout),
        }
        key = (api_key or "").strip()
        secret = _expand_env_newlines(api_secret or "").strip()
        if key and secret:
            opts["apiKey"] = key
            opts["secret"] = secret
            if api_passphrase:
                opts["password"] = api_passphrase
            log.info(
                "futures market using authenticated Coinbase",
                extra={"data": {"allow_orders": self._allow_orders}},
            )
        else:
            log.info("futures market using public Coinbase (marks only)")
        self._exchange = ccxt.coinbase(opts)

    @property
    def exchange(self) -> object:
        return self._exchange

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        symbol = to_futures_ccxt_symbol(product)
        rows = self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)  # type: ignore[attr-defined]
        return [list(map(float, row[:6])) for row in rows]

    def fetch_ticker(self, product: str) -> Ticker:
        pid = normalize_futures_product(product)
        symbol = to_futures_ccxt_symbol(pid)
        raw = self._exchange.fetch_ticker(symbol)  # type: ignore[attr-defined]
        last = raw.get("last")
        bid = raw.get("bid")
        ask = raw.get("ask")
        ts_ms = raw.get("timestamp")
        if ts_ms:
            ts = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)
        return Ticker(
            product=pid,
            last=float(last) if last is not None else None,
            bid=float(bid) if bid is not None else None,
            ask=float(ask) if ask is not None else None,
            ts=ts,
        )

    def fetch_balance_raw(self) -> dict[str, Any]:
        return self._exchange.fetch_balance()  # type: ignore[attr-defined]

    def fetch_account_value_usd(
        self, *, crypto_marks: dict[str, float] | None = None
    ) -> float:
        bal = self.fetch_balance_raw()
        marks = dict(crypto_marks or {})
        totals = bal.get("total") if isinstance(bal.get("total"), dict) else {}
        # Price any non-stable balances not already marked (so FT budget
        # includes open crypto MTM even before the crypto engine ticks).
        for ccy, amt in (totals or {}).items():
            if str(ccy).upper() in _STABLES:
                continue
            try:
                qty = float(amt or 0.0)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            product = f"{str(ccy).upper()}-USD"
            if product in marks and marks[product] > 0:
                continue
            # Already matched via base in estimate; still need a price
            have = False
            for prod, px in marks.items():
                if prod.split("-")[0].upper() == str(ccy).upper() and px > 0:
                    have = True
                    break
            if have:
                continue
            try:
                raw = self._exchange.fetch_ticker(f"{ccy}/USD")  # type: ignore[attr-defined]
                last = raw.get("last")
                if last is not None and float(last) > 0:
                    marks[product] = float(last)
            except Exception:
                try:
                    raw = self._exchange.fetch_ticker(f"{ccy}/USDC")  # type: ignore[attr-defined]
                    last = raw.get("last")
                    if last is not None and float(last) > 0:
                        marks[product] = float(last)
                except Exception:
                    log.debug("no mark for balance asset %s", ccy)
        return estimate_account_value_usd(bal, crypto_marks=marks)

    def create_swap_market_order(
        self,
        product: str,
        side: str,
        amount: float,
        *,
        leverage: float = 1.0,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """Place INTX/perp market order via ccxt (base_size). Requires allow_orders."""
        if not self._allow_orders:
            raise RuntimeError("futures create_swap_market_order refused: allow_orders=False")
        if amount <= 0:
            raise ValueError("amount must be positive")
        symbol = to_futures_ccxt_symbol(product)
        side_l = (side or "").lower()
        params: dict[str, Any] = {"leverage": float(leverage)}
        if reduce_only:
            params["reduceOnly"] = True
        # Prefer plain create_order with base amount for swaps (not spot quote_size quirks)
        order = self._exchange.create_order(  # type: ignore[attr-defined]
            symbol, "market", side_l, float(amount), None, params
        )
        return self._settle_order(order if isinstance(order, dict) else {}, symbol)

    def _settle_order(self, order: dict[str, Any], symbol: str) -> dict[str, Any]:
        fill_px, fill_qty, _fee = parse_futures_order_fill(order)
        if fill_px > 0 and fill_qty > 0:
            return order
        order_id = order.get("id")
        if not order_id and isinstance(order.get("info"), dict):
            order_id = order["info"].get("order_id")
        if not order_id:
            return order
        fetch = getattr(self._exchange, "fetch_order", None)
        if not callable(fetch):
            return order
        last: dict[str, Any] = order
        for attempt in range(6):
            time.sleep(0.35 * (attempt + 1))
            try:
                fetched = fetch(str(order_id), symbol)
            except Exception:
                log.exception("futures fetch_order failed while settling %s", order_id)
                return last
            if isinstance(fetched, dict):
                last = fetched
                fill_px, fill_qty, _fee = parse_futures_order_fill(last)
                if fill_px > 0 and fill_qty > 0:
                    return last
                status = str(last.get("status") or "").lower()
                if status in ("canceled", "cancelled", "rejected", "expired"):
                    return last
        return last
