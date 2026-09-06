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



def make_broker(settings: Settings, exchange: object | None = None) -> LiveBroker | None:
    """Factory used by the engine. Paper (None) is the only default."""
    if settings.live_orders_permitted():
        return LiveBroker(settings, exchange=exchange)
    if settings.mode == "live":
        raise LiveTradingRefused(
            "Live trading refused: MODE=live but LIVE_ENABLED is not true."
        )
    return None
