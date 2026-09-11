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
