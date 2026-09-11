"""CFM CDE product map + integer contract sizing floor."""

from __future__ import annotations

import pytest

from snowball.futures.market import (
    DEFAULT_FUTURES_PRODUCTS,
    is_cfm_product,
    normalize_futures_product,
    order_size_for_product,
    to_futures_ccxt_symbol,
)
from snowball.sizing import cfm_contract_count, cfm_required_margin_usd


def test_default_products_are_cfm_cde() -> None:
    assert DEFAULT_FUTURES_PRODUCTS == ("US5-19DEC30-CDE", "TEK-19DEC30-CDE")
    assert all(is_cfm_product(p) for p in DEFAULT_FUTURES_PRODUCTS)


def test_spy_qqq_aliases_map_to_cfm() -> None:
    assert normalize_futures_product("SPY") == "US5-19DEC30-CDE"
    assert normalize_futures_product("SPY-PERP-INTX") == "US5-19DEC30-CDE"
    assert normalize_futures_product("QQQ-PERP-INTX") == "TEK-19DEC30-CDE"
    assert to_futures_ccxt_symbol("US5-19DEC30-CDE").startswith("CDEUS5/")
    assert to_futures_ccxt_symbol("TEK-19DEC30-CDE").startswith("CDETEK/")


def test_cfm_contract_count_floor_and_cap() -> None:
    # US500-ish price; 10% margin → ~$306 required for 1 contract
    price = 3061.0
    req = cfm_required_margin_usd(price, contracts=1, leverage=1.0, margin_rate=0.10)
    assert req == pytest.approx(306.1, rel=1e-6)

    assert cfm_contract_count(
        price=price,
        budget_usd=100.0,
        available_margin_usd=10_000.0,
        max_contracts=1,
        margin_rate=0.10,
    ) == 0  # cannot fund 1

    assert cfm_contract_count(
        price=price,
        budget_usd=500.0,
        available_margin_usd=500.0,
        max_contracts=1,
        margin_rate=0.10,
    ) == 1

    assert cfm_contract_count(
        price=price,
        budget_usd=50_000.0,
        available_margin_usd=50_000.0,
        max_contracts=1,
        margin_rate=0.10,
    ) == 1  # hard cap

    assert cfm_contract_count(
        price=price,
        budget_usd=50_000.0,
        available_margin_usd=50_000.0,
        max_contracts=3,
        margin_rate=0.10,
    ) == 3


def test_order_size_for_product_integer_cfm_vs_frac_intx() -> None:
    cfm_amt = order_size_for_product(
        "US5-19DEC30-CDE",
        notional_usd=500.0,
        price=3061.0,
        available_margin_usd=500.0,
        max_contracts=1,
        leverage=1.0,
        margin_rate=0.10,
    )
    assert cfm_amt == 1.0

    intx_amt = order_size_for_product(
        "AAPL-PERP-INTX",
        notional_usd=100.0,
        price=200.0,
        available_margin_usd=100.0,
        max_contracts=1,
        leverage=1.0,
    )
    assert intx_amt == pytest.approx(0.5)


def test_cfm_sizing_budget_floors_per_leg_soft_cap() -> None:
    """effective_per_leg ~$118 must not zero a 1-contract CFM order when MAX_NOTIONAL=4000."""
    from snowball.sizing import cfm_sizing_budget_usd

    price = 3061.0
    # Tiny per-leg soft cap historically passed as budget_usd
    budget = cfm_sizing_budget_usd(
        118.0,
        price=price,
        lane_max_notional_usd=4000.0,
        leverage=1.0,
        margin_rate=0.10,
        max_contracts=1,
    )
    assert budget >= 4000.0
    amt = order_size_for_product(
        "US5-19DEC30-CDE",
        notional_usd=118.0,
        price=price,
        available_margin_usd=118.0,
        max_contracts=1,
        leverage=1.0,
        margin_rate=0.10,
        lane_max_notional_usd=4000.0,
    )
    assert amt == 1.0

    tek = order_size_for_product(
        "TEK-19DEC30-CDE",
        notional_usd=50.0,
        price=2200.0,
        available_margin_usd=50.0,
        max_contracts=1,
        leverage=1.0,
        margin_rate=0.10,
        lane_max_notional_usd=4000.0,
    )
    assert tek == 1.0


def test_cfm_tick_sizes() -> None:
    from snowball.futures.market import tick_size_for_product

    assert tick_size_for_product("TEK-19DEC30-CDE") == 1.0
    assert tick_size_for_product("TEK") == 1.0
    assert tick_size_for_product("US5-19DEC30-CDE") == 0.1
    assert tick_size_for_product("US5") == 0.1


def test_tek_us5_maker_buy_from_book_never_crosses_ask() -> None:
    """2026-09-11 live bug: TEK post-only at 3947 crossed a 1-point book."""
    from snowball.maker import maker_buy_price, round_to_tick

    # 1-tick TEK spread: inside-spread 3946.25 must floor to 3946, not ROUND to 3947.
    tek = maker_buy_price(3946.0, 3947.0, tick=1.0)
    assert tek == 3946.0
    assert tek < 3947.0
    assert round_to_tick(3946.25, 1.0, direction="down") == 3946.0

    tek_frac = maker_buy_price(3946.4, 3947.0, tick=1.0)
    assert tek_frac == 3946.0

    # Locked/at-ask book: refuse rather than post-only at 3947.
    assert maker_buy_price(3947.0, 3947.0, tick=1.0) is None

    us5 = maker_buy_price(3087.6, 3087.8, tick=0.1)
    assert us5 is not None
    assert 3087.6 <= us5 < 3087.8
    # Snapped to 0.1 tick and strictly below ask (US5 filled 3087.7 this morning).
    snapped = round_to_tick(us5, 0.1, direction="down")
    assert snapped == pytest.approx(us5)
    assert snapped < 3087.8


def test_cfm_maker_limit_uses_that_product_book_not_caller_price() -> None:
    """Stale 3947 / US5 BBA must not be posted on TEK; each CDE uses its own book."""
    from snowball.futures.market import CoinbaseFuturesMarket

    class Ex:
        def __init__(self) -> None:
            self.orders: list[dict] = []
            self.books = {
                "CDEUS5/USD:USD-301219": {
                    "bids": [[3087.6, 1.0]],
                    "asks": [[3087.8, 1.0]],
                },
                "CDETEK/USD:USD-301219": {
                    "bids": [[3946.0, 1.0]],
                    "asks": [[3947.0, 1.0]],
                },
            }

        def fetch_order_book(self, symbol: str, limit: int = 5) -> dict:
            return dict(self.books[symbol])

        def create_order(self, symbol, typ, side, amount, price, params):
            self.orders.append(
                {
                    "symbol": symbol,
                    "type": typ,
                    "side": side,
                    "amount": amount,
                    "price": float(price),
                    "params": dict(params or {}),
                }
            )
            return {
                "id": f"o-{len(self.orders)}",
                "filled": float(amount),
                "average": float(price),
                "price": float(price),
                "remaining": 0.0,
                "status": "closed",
                "fee": {"cost": 0.0, "currency": "USD"},
            }

    ex = Ex()
    mkt = CoinbaseFuturesMarket(exchange=ex, allow_orders=True)

    # Reproduce this morning: caller hands TEK the crossing 3947 (and even US5 BBA).
    mkt.create_swap_maker_limit_order(
        "TEK-19DEC30-CDE",
        "buy",
        1.0,
        price=3947.0,
        bid=3087.6,
        ask=3087.8,
        leverage=1.0,
        timeout_sec=1.0,
    )
    tek_ord = ex.orders[-1]
    assert tek_ord["symbol"].startswith("CDETEK/")
    assert tek_ord["amount"] == 1.0
    assert tek_ord["price"] == 3946.0
    assert tek_ord["price"] != 3947.0
    assert tek_ord["params"].get("postOnly") is True
    assert tek_ord["params"].get("leverage") == "1"

    mkt.create_swap_maker_limit_order(
        "US5-19DEC30-CDE",
        "buy",
        1.0,
        price=3947.0,  # wrong on purpose
        leverage=1.0,
        timeout_sec=1.0,
    )
    us5_ord = ex.orders[-1]
    assert us5_ord["symbol"].startswith("CDEUS5/")
    assert us5_ord["amount"] == 1.0
    assert 3087.6 <= us5_ord["price"] < 3087.8
    assert us5_ord["price"] != 3947.0
    assert us5_ord["params"].get("postOnly") is True
