"""Coinbase CFM CDE futures marks + dual-gated Advanced Trade FUTURE orders.

Future Trader / Crash Guard / Fed Desk trade CFM index perps on Coinbase
Derivatives Exchange (product ids ``*-CDE``). Stock lane may still share the
INTX ``*-PERP-INTX`` helpers below for single-name equity perps.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

from snowball.models import Ticker

log = logging.getLogger("snowball.futures.market")

# CFM CDE index perps (verified via public product_type=FUTURE list).
# ccxt unified symbols include the Dec-2030 expiry suffix Coinbase currently lists.
PRODUCT_TO_CCXT: dict[str, str] = {
    "US5-19DEC30-CDE": "CDEUS5/USD:USD-301219",  # display: US 500 PERP
    "TEK-19DEC30-CDE": "CDETEK/USD:USD-301219",  # display: TECH PERP / Tech100
}

# Human / legacy aliases → canonical CFM product id
PRODUCT_ALIASES: dict[str, str] = {
    "SPY": "US5-19DEC30-CDE",
    "SPY-PERP": "US5-19DEC30-CDE",
    "SPY-PERP-INTX": "US5-19DEC30-CDE",
    "US5": "US5-19DEC30-CDE",
    "US500": "US5-19DEC30-CDE",
    "US-500": "US5-19DEC30-CDE",
    "US500PERP": "US5-19DEC30-CDE",
    "QQQ": "TEK-19DEC30-CDE",
    "QQQ-PERP": "TEK-19DEC30-CDE",
    "QQQ-PERP-INTX": "TEK-19DEC30-CDE",
    "TEK": "TEK-19DEC30-CDE",
    "TECH": "TEK-19DEC30-CDE",
    "TECH100": "TEK-19DEC30-CDE",
    "TECH-PERP": "TEK-19DEC30-CDE",
}

DEFAULT_FUTURES_PRODUCTS: tuple[str, ...] = (
    "US5-19DEC30-CDE",
    "TEK-19DEC30-CDE",
)

CFM_DISPLAY_NAMES: dict[str, str] = {
    "US5-19DEC30-CDE": "US 500 PERP",
    "TEK-19DEC30-CDE": "TECH PERP",
}

_STABLES = ("USD", "USDC", "USDT")


def _expand_env_newlines(value: str) -> str:
    """Turn .env-style \\n escapes into real newlines (CDP PEM). Local copy — no live import."""
    return (value or "").replace("\\n", "\n").replace("\\r", "\r")


def leverage_param(leverage: float | int | str) -> str:
    """Coinbase create_order expects leverage as a string (e.g. "1"), not float 1.0."""
    return str(int(float(leverage)))


def is_cfm_product(product: str) -> bool:
    """True for Coinbase Financial Markets CDE futures (``*-CDE``)."""
    p = (product or "").strip().upper().replace("_", "-")
    if p in PRODUCT_ALIASES:
        p = PRODUCT_ALIASES[p]
    if p in PRODUCT_TO_CCXT:
        return True
    return p.endswith("-CDE")


def cfm_display_name(product: str) -> str:
    pid = normalize_futures_product(product)
    return CFM_DISPLAY_NAMES.get(pid, pid)


def normalize_futures_product(product: str) -> str:
    """Normalize to Coinbase product id (CFM ``*-CDE`` or INTX ``*-PERP-INTX``)."""
    p = product.strip().upper().replace("_", "-")
    if p in PRODUCT_ALIASES:
        return PRODUCT_ALIASES[p]
    if p in PRODUCT_TO_CCXT:
        return p
    for pid, unified in PRODUCT_TO_CCXT.items():
        if p == unified.upper() or p.replace("/", "-") == unified.upper().replace("/", "-"):
            return pid
        # CDEUS5 / CDETEK base forms
        base = unified.split("/")[0].upper()
        if p == base or p == base.replace("CDE", ""):
            return pid
    if p.endswith("-CDE"):
        return p
    # Generic INTX equity/crypto perps (stock lane): BASE or BASE-PERP → BASE-PERP-INTX
    if p.endswith("-PERP-INTX"):
        return p
    if p.endswith("-PERP"):
        return f"{p}-INTX"
    if "/" in p:
        return p
    if p.isalpha() or (p.replace("-", "").isalnum() and "-" not in p):
        # Bare tickers that are index aliases already handled; others → INTX stock form
        return f"{p}-PERP-INTX"
    return p


def to_futures_ccxt_symbol(product: str) -> str:
    """Map Coinbase product id to ccxt unified symbol.

    CFM CDE ids use ``CDE*/USD:USD-YYMMDD``. INTX ``{BASE}-PERP-INTX`` stays
    ``{BASE}/USDC:USDC`` for the stock lane.
    """
    pid = normalize_futures_product(product)
    if pid in PRODUCT_TO_CCXT:
        return PRODUCT_TO_CCXT[pid]
    if "/" in pid:
        return pid
    if pid.endswith("-CDE"):
        # Fallback: try id as-is via markets later; synthesize common form
        code = pid.split("-")[0]
        return f"CDE{code}/USD:USD-301219"
    if pid.endswith("-PERP-INTX"):
        base = pid[: -len("-PERP-INTX")]
        if base:
            return f"{base}/USDC:USDC"
    raise ValueError(
        f"unknown futures product {product!r}; expected *-CDE or *-PERP-INTX "
        f"(known CFM: {sorted(PRODUCT_TO_CCXT)})"
    )


def order_size_for_product(
    product: str,
    *,
    notional_usd: float,
    price: float,
    available_margin_usd: float,
    max_contracts: int = 1,
    leverage: float = 1.0,
    margin_rate: float = 0.10,
) -> float:
    """Return order amount: integer CFM contracts, else INTX base-coin size."""
    from snowball.sizing import cfm_contract_count

    if price <= 0:
        return 0.0
    if is_cfm_product(product):
        # Budget gate uses the larger of notional allotment and available margin
        # so a tiny per-leg soft cap cannot fractionalize CDE size.
        budget = max(0.0, float(notional_usd))
        return float(
            cfm_contract_count(
                price=float(price),
                budget_usd=budget,
                available_margin_usd=float(available_margin_usd),
                max_contracts=int(max_contracts),
                leverage=float(leverage),
                margin_rate=float(margin_rate),
            )
        )
    return round_amount_down(float(notional_usd) / float(price), 0.01)


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
    """Coinbase CFM/INTX OHLCV/tickers; live FUTURE orders only when caller dual-gates."""

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

    def fetch_bba(self, product: str) -> tuple[float | None, float | None]:
        """Best bid/ask from order book (preferred) or ticker."""
        from snowball.maker import bba_from_order_book

        symbol = to_futures_ccxt_symbol(product)
        fetch_ob = getattr(self._exchange, "fetch_order_book", None)
        if callable(fetch_ob):
            try:
                book = fetch_ob(symbol, 5)
                bid, ask = bba_from_order_book(book if isinstance(book, dict) else None)
                if bid is not None or ask is not None:
                    return bid, ask
            except Exception:
                log.exception("futures fetch_order_book failed for %s", product)
        fetch_t = getattr(self._exchange, "fetch_ticker", None)
        if callable(fetch_t):
            try:
                raw = fetch_t(symbol)
                if isinstance(raw, dict):
                    bid = raw.get("bid")
                    ask = raw.get("ask")
                    return (
                        float(bid) if bid is not None and float(bid) > 0 else None,
                        float(ask) if ask is not None and float(ask) > 0 else None,
                    )
            except Exception:
                log.exception("futures fetch_ticker failed for %s", product)
        return None, None

    def cancel_order_safe(self, order_id: str, symbol: str) -> None:
        cancel = getattr(self._exchange, "cancel_order", None)
        if not callable(cancel) or not order_id:
            return
        try:
            cancel(str(order_id), symbol)
        except Exception:
            log.exception("futures cancel_order failed for %s", order_id)

    def create_swap_market_order(
        self,
        product: str,
        side: str,
        amount: float,
        *,
        leverage: float = 1.0,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """Place FUTURE/perp market order via ccxt (base_size). Requires allow_orders.

        CFM ``*-CDE`` uses integer contracts. Prefer maker limit for entries;
        market remains for emergency flatten and urgent FT session-close fallback.
        """
        if not self._allow_orders:
            raise RuntimeError("futures create_swap_market_order refused: allow_orders=False")
        if amount <= 0:
            raise ValueError("amount must be positive")
        pid = normalize_futures_product(product)
        symbol = to_futures_ccxt_symbol(pid)
        side_l = (side or "").lower()
        cfm = is_cfm_product(pid)
        # CFM CDE: integer contracts. INTX swaps: fractional base_size OK.
        qty = float(int(round(float(amount)))) if cfm else float(amount)
        if qty <= 0:
            raise ValueError("amount must be positive")
        params: dict[str, Any] = {"leverage": leverage_param(leverage)}
        # reduceOnly is INTX-oriented; FCM often rejects REDUCE_ONLY_NOT_ALLOWED_ON_VENUE
        if reduce_only and not cfm:
            params["reduceOnly"] = True
        order = self._exchange.create_order(  # type: ignore[attr-defined]
            symbol, "market", side_l, qty, None, params
        )
        return self._settle_order(order if isinstance(order, dict) else {}, symbol)

    def create_swap_maker_limit_order(
        self,
        product: str,
        side: str,
        amount: float,
        *,
        price: float | None = None,
        bid: float | None = None,
        ask: float | None = None,
        leverage: float = 1.0,
        reduce_only: bool = False,
        timeout_sec: float | None = None,
        post_only: bool = True,
    ) -> dict[str, Any]:
        """FUTURE/perp GTC post-only limit (price required). Settle or cancel on timeout."""
        from snowball.maker import (
            DEFAULT_MAKER_TIMEOUT_SEC,
            maker_buy_price,
            maker_sell_price,
        )

        if not self._allow_orders:
            raise RuntimeError(
                "futures create_swap_maker_limit_order refused: allow_orders=False"
            )
        if amount <= 0:
            raise ValueError("amount must be positive")
        side_l = (side or "").lower()
        if side_l not in ("buy", "sell"):
            raise ValueError(f"invalid side {side!r}")

        if bid is None and ask is None:
            bid, ask = self.fetch_bba(product)

        if price is not None and float(price) > 0:
            limit_px = float(price)
        elif side_l == "buy":
            limit_px = maker_buy_price(bid, ask)
        else:
            limit_px = maker_sell_price(bid, ask)
        if limit_px is None or limit_px <= 0:
            raise ValueError(
                f"maker swap {side_l} refused: no usable book price "
                f"(bid={bid!r} ask={ask!r})"
            )
        if side_l == "buy" and ask is not None and limit_px >= float(ask):
            if bid is None or float(bid) <= 0:
                raise ValueError("maker swap buy would cross ask; refused")
            limit_px = float(bid)
            if limit_px >= float(ask):
                raise ValueError("maker swap buy would cross ask; refused")
        if side_l == "sell" and bid is not None and limit_px <= float(bid):
            if ask is None or float(ask) <= 0:
                raise ValueError("maker swap sell would cross bid; refused")
            limit_px = float(ask)
            if limit_px <= float(bid):
                raise ValueError("maker swap sell would cross bid; refused")

        pid = normalize_futures_product(product)
        symbol = to_futures_ccxt_symbol(pid)
        cfm = is_cfm_product(pid)
        qty = float(int(round(float(amount)))) if cfm else float(amount)
        if qty <= 0:
            raise ValueError("amount must be positive")
        params: dict[str, Any] = {
            "leverage": leverage_param(leverage),
            "timeInForce": "GTC",
        }
        if reduce_only and not cfm:
            params["reduceOnly"] = True
        if post_only:
            params["postOnly"] = True
        # Limit create_order requires price (CFM contracts + INTX swaps)
        order = self._exchange.create_order(  # type: ignore[attr-defined]
            symbol, "limit", side_l, qty, float(limit_px), params
        )
        wait = (
            DEFAULT_MAKER_TIMEOUT_SEC
            if timeout_sec is None
            else max(1.0, float(timeout_sec))
        )
        return self._settle_maker_order(
            order if isinstance(order, dict) else {},
            symbol,
            timeout_sec=wait,
        )

    def _settle_maker_order(
        self, order: dict[str, Any], symbol: str, *, timeout_sec: float
    ) -> dict[str, Any]:
        fill_px, fill_qty, _fee = parse_futures_order_fill(order)
        status = str(order.get("status") or "").lower()
        if fill_px > 0 and fill_qty > 0 and status in ("closed", "filled"):
            return order
        remaining = order.get("remaining")
        if (
            fill_px > 0
            and fill_qty > 0
            and remaining is not None
            and float(remaining) <= 0
        ):
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
        deadline = time.monotonic() + float(timeout_sec)
        attempt = 0
        while time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.05, 0.25 * (attempt + 1))))
            attempt += 1
            try:
                fetched = fetch(str(order_id), symbol)
            except Exception:
                log.exception(
                    "futures fetch_order failed while settling maker %s", order_id
                )
                break
            if isinstance(fetched, dict):
                last = fetched
                fill_px, fill_qty, _fee = parse_futures_order_fill(last)
                status = str(last.get("status") or "").lower()
                if status in ("canceled", "cancelled", "rejected", "expired"):
                    return last
                remaining = last.get("remaining")
                if fill_qty > 0 and fill_px > 0 and (
                    status in ("closed", "filled")
                    or (remaining is not None and float(remaining) <= 0)
                ):
                    return last

        self.cancel_order_safe(str(order_id), symbol)
        if callable(fetch):
            try:
                fetched = fetch(str(order_id), symbol)
                if isinstance(fetched, dict):
                    last = fetched
            except Exception:
                log.exception("futures post-cancel fetch_order failed for %s", order_id)
        return last

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
