from __future__ import annotations

import logging
from typing import Any

from snowball.config import LiveTradingRefused, Settings

log = logging.getLogger("snowball.live")


def _expand_env_newlines(value: str) -> str:
    """Turn .env-style \\n escapes into real newlines (CDP PEM secrets)."""
    return (value or "").replace('\\n', '\n').replace('\\r', '\r')


# Public alias used by tests / callers
expand_pem_newlines = _expand_env_newlines


def parse_order_fill(order: dict[str, Any]) -> tuple[float, float, float]:
    """Return (fill_px, fill_qty, fee_usd) from a ccxt order response."""
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


class LiveBroker:
    """Hard-gated live path. Construction fails unless MODE=live AND LIVE_ENABLED=true.

    Default config never reaches ccxt create_order.
    """

    def __init__(self, settings: Settings, exchange: object | None = None) -> None:
        if not settings.live_orders_permitted():
            raise LiveTradingRefused(
                "Live trading refused: require MODE=live AND LIVE_ENABLED=true. "
                f"Got MODE={settings.mode!r} LIVE_ENABLED={settings.live_enabled}."
            )
        if not settings.coinbase_api_key or not settings.coinbase_api_secret:
            raise LiveTradingRefused("Live trading refused: missing Coinbase API key/secret.")
        if exchange is not None:
            self._exchange = exchange
        else:
            import ccxt

            secret = _expand_env_newlines(settings.coinbase_api_secret)
            klass = getattr(ccxt, settings.exchange_id)
            self._exchange = klass(
                {
                    "apiKey": settings.coinbase_api_key,
                    "secret": secret,
                    "password": settings.coinbase_api_passphrase or None,
                    "enableRateLimit": True,
                    "rateLimit": 250,
                }
            )
        log.warning("LIVE broker constructed — real orders are possible")

    @property
    def exchange(self) -> object:
        return self._exchange

    def fetch_free_usd(self) -> float:
        """Free USD (or USDC/USDT) balance available for buys."""
        bal = self._exchange.fetch_balance()  # type: ignore[attr-defined]
        free = (bal or {}).get("free") or {}
        if isinstance(free, dict):
            for key in ("USD", "USDC", "USDT"):
                if free.get(key) is not None:
                    return float(free[key])
        if isinstance(bal, dict):
            for key in ("USD", "USDC", "USDT"):
                block = bal.get(key)
                if isinstance(block, dict) and block.get("free") is not None:
                    return float(block["free"])
        return 0.0

    def create_market_order(
        self,
        product: str,
        side: str,
        amount: float,
        *,
        price: float | None = None,
        cost: float | None = None,
    ) -> dict:
        """Place a market order.

        Coinbase Advanced Trade spot *buys* are quote-notional (quote_size).
        Prefer cost (USD to spend) via create_market_buy_order_with_cost;
        otherwise pass price so ccxt can compute amount * price.
        Market *sells* use base amount only.

        Coinbase create responses often omit fills; we settle via fetch_order.
        """
        from snowball.market import to_ccxt_symbol

        symbol = to_ccxt_symbol(product)
        side_l = (side or '').lower()
        if side_l == 'buy':
            if cost is not None and float(cost) > 0:
                create_with_cost = getattr(
                    self._exchange, 'create_market_buy_order_with_cost', None
                )
                if callable(create_with_cost):
                    order = create_with_cost(symbol, float(cost))
                else:
                    order = self._exchange.create_order(
                        symbol,
                        'market',
                        'buy',
                        float(cost),
                        None,
                        {'createMarketBuyOrderRequiresPrice': False},
                    )
                return self._settle_order(order if isinstance(order, dict) else {}, symbol)
            if price is not None and float(price) > 0:
                order = self._exchange.create_order(
                    symbol, 'market', 'buy', amount, float(price)
                )
                return self._settle_order(order if isinstance(order, dict) else {}, symbol)
            raise ValueError(
                'Coinbase spot market buy requires cost (quote notional) or price'
            )
        order = self._exchange.create_order(symbol, 'market', side_l, amount)
        return self._settle_order(order if isinstance(order, dict) else {}, symbol)

    def _settle_order(self, order: dict, symbol: str) -> dict:
        """Coinbase create_order often returns an id without fills; fetch until filled."""
        fill_px, fill_qty, _fee = parse_order_fill(order)
        if fill_px > 0 and fill_qty > 0:
            return order
        order_id = order.get('id')
        if not order_id and isinstance(order.get('info'), dict):
            order_id = order['info'].get('order_id')
        if not order_id:
            return order
        fetch = getattr(self._exchange, 'fetch_order', None)
        if not callable(fetch):
            return order
        import time

        last: dict = order
        for attempt in range(6):
            time.sleep(0.35 * (attempt + 1))
            try:
                fetched = fetch(str(order_id), symbol)
            except Exception:
                log.exception('fetch_order failed while settling %s', order_id)
                return last
            if isinstance(fetched, dict):
                last = fetched
                fill_px, fill_qty, _fee = parse_order_fill(last)
                if fill_px > 0 and fill_qty > 0:
                    return last
                status = str(last.get('status') or '').lower()
                if status in ('canceled', 'cancelled', 'rejected', 'expired'):
                    return last
        return last



    def fetch_bba(self, product: str) -> tuple[float | None, float | None]:
        """Best bid/ask from order book (preferred) or ticker."""
        from snowball.maker import bba_from_order_book
        from snowball.market import to_ccxt_symbol

        symbol = to_ccxt_symbol(product)
        fetch_ob = getattr(self._exchange, "fetch_order_book", None)
        if callable(fetch_ob):
            try:
                book = fetch_ob(symbol, 5)
                bid, ask = bba_from_order_book(book if isinstance(book, dict) else None)
                if bid is not None or ask is not None:
                    return bid, ask
            except Exception:
                log.exception("fetch_order_book failed for %s", product)
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
                log.exception("fetch_ticker failed for %s", product)
        return None, None

    def cancel_order_safe(self, order_id: str, symbol: str) -> None:
        cancel = getattr(self._exchange, "cancel_order", None)
        if not callable(cancel) or not order_id:
            return
        try:
            cancel(str(order_id), symbol)
        except Exception:
            log.exception("cancel_order failed for %s", order_id)

    def create_maker_limit_order(
        self,
        product: str,
        side: str,
        amount: float,
        *,
        price: float | None = None,
        bid: float | None = None,
        ask: float | None = None,
        timeout_sec: float | None = None,
        post_only: bool = True,
    ) -> dict:
        """Place a GTC/post-only limit that rests as maker; settle or cancel on timeout.

        Buy requires a bid (or explicit price below ask). Sell requires an ask
        (or explicit price above bid). Does not place market orders. Unfilled
        after timeout → cancel; partial fills are returned as-is (filled qty).
        """
        from snowball.maker import (
            DEFAULT_MAKER_TIMEOUT_SEC,
            maker_buy_price,
            maker_sell_price,
        )
        from snowball.market import to_ccxt_symbol

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
                f"maker limit {side_l} refused: no usable book price "
                f"(bid={bid!r} ask={ask!r})"
            )
        # Never cross the book when we can see the far side.
        if side_l == "buy" and ask is not None and limit_px >= float(ask):
            limit_px = float(bid) if bid is not None and float(bid) > 0 else limit_px
            if ask is not None and limit_px >= float(ask):
                raise ValueError("maker buy would cross ask; refused")
        if side_l == "sell" and bid is not None and limit_px <= float(bid):
            limit_px = float(ask) if ask is not None and float(ask) > 0 else limit_px
            if bid is not None and limit_px <= float(bid):
                raise ValueError("maker sell would cross bid; refused")

        symbol = to_ccxt_symbol(product)
        params: dict[str, object] = {"timeInForce": "GTC"}
        if post_only:
            params["postOnly"] = True
        order = self._exchange.create_order(  # type: ignore[attr-defined]
            symbol, "limit", side_l, float(amount), float(limit_px), params
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
        self, order: dict, symbol: str, *, timeout_sec: float
    ) -> dict:
        """Poll until filled/canceled or timeout; cancel residual on timeout."""
        import time

        fill_px, fill_qty, _fee = parse_order_fill(order)
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

        last: dict = order
        deadline = time.monotonic() + float(timeout_sec)
        attempt = 0
        while time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.05, 0.25 * (attempt + 1))))
            attempt += 1
            try:
                fetched = fetch(str(order_id), symbol)
            except Exception:
                log.exception("fetch_order failed while settling maker %s", order_id)
                break
            if isinstance(fetched, dict):
                last = fetched
                fill_px, fill_qty, _fee = parse_order_fill(last)
                status = str(last.get("status") or "").lower()
                if status in ("canceled", "cancelled", "rejected", "expired"):
                    return last
                remaining = last.get("remaining")
                if fill_qty > 0 and fill_px > 0 and (
                    status in ("closed", "filled")
                    or (remaining is not None and float(remaining) <= 0)
                ):
                    return last

        # Timeout: cancel; then fetch once for any partial fill.
        self.cancel_order_safe(str(order_id), symbol)
        if callable(fetch):
            try:
                fetched = fetch(str(order_id), symbol)
                if isinstance(fetched, dict):
                    last = fetched
            except Exception:
                log.exception("post-cancel fetch_order failed for %s", order_id)
        return last



def make_broker(settings: Settings, exchange: object | None = None) -> LiveBroker | None:
    """Factory used by the engine. Paper (None) is the only default."""
    if settings.live_orders_permitted():
        return LiveBroker(settings, exchange=exchange)
    if settings.mode == "live":
        raise LiveTradingRefused(
            "Live trading refused: MODE=live but LIVE_ENABLED is not true."
        )
    return None
