"""Soft-reserve treasury BTC out of CRYPTO deployable budget + hard-block sells."""

from __future__ import annotations

from pathlib import Path

import pytest

from snowball.allocation import crypto_allocation_base_usd, lane_budgets_usd
from snowball.config import Settings
from snowball.gates import never_sell_treasury_allowed
from snowball.treasury.ledger import ensure_schema, record_contribution
from snowball.treasury.reserve import (
    treasury_btc_sell_allowed,
    treasury_soft_reserve,
)


def test_crypto_allocation_base_subtracts_reserve() -> None:
    assert crypto_allocation_base_usd(1406.05, 608.70) == pytest.approx(797.35)
    assert crypto_allocation_base_usd(500.0, 608.70) == 0.0
    assert crypto_allocation_base_usd(1000.0, 0.0) == 1000.0


def test_lane_budgets_crypto_uses_reserve(tmp_path: Path) -> None:
    av = 1406.05
    reserve = 608.70
    budgets = lane_budgets_usd(av, treasury_reserve_usd=reserve)
    assert budgets["account_value_usd"] == av
    assert budgets["treasury_reserve_usd"] == reserve
    assert budgets["crypto_allocation_base_usd"] == pytest.approx(797.35)
    # CRYPTO 40% of reserved base — drops vs gross 40% of full AV
    assert budgets["crypto_usd"] == pytest.approx(797.35 * 0.40)
    assert budgets["crypto_usd"] == pytest.approx(av * 0.40 - reserve * 0.40)
    # Stock / FT still size off full AV (treasury is BTC CRYPTO backstop)
    assert budgets["stock_usd"] == pytest.approx(av * 0.20)
    assert budgets["futures_usd"] == pytest.approx(av * 0.40)


def test_soft_reserve_reads_treasury_db(tmp_path: Path) -> None:
    db = tmp_path / "snowball_treasury.db"
    ensure_schema(db)
    record_contribution(
        db,
        usd_amount=620.32,
        btc_qty=0.00731916,
        btc_price=84752.8951409725,
        kind="seed",
        note="test",
    )
    res = treasury_soft_reserve(db, mark_btc_usd=83165.695)
    assert res["btc_qty"] == pytest.approx(0.00731916)
    assert res["cost_usd"] == pytest.approx(620.32)
    assert res["mark_usd"] == pytest.approx(0.00731916 * 83165.695)
    assert res["reserve_usd"] == pytest.approx(res["mark_usd"])
    # Allocation base drops by ~treasury mark (~$609)
    base = crypto_allocation_base_usd(1406.05, res["reserve_usd"])
    assert 1406.05 - base == pytest.approx(res["reserve_usd"])
    assert res["reserve_usd"] == pytest.approx(608.70, abs=0.05)


def test_never_sell_treasury_hard_block() -> None:
    ok, why = treasury_btc_sell_allowed(
        product="BTC-USD",
        sell_qty=0.001,
        wallet_btc=0.00731916,
        reserved_btc=0.00731916,
    )
    assert ok is False and why == "never_sell_treasury"

    ok, why = treasury_btc_sell_allowed(
        product="BTC-USD",
        sell_qty=0.001,
        wallet_btc=0.00831916,
        reserved_btc=0.00731916,
    )
    assert ok is True and why == "ok"

    ok, why = treasury_btc_sell_allowed(
        product="ETH-USD",
        sell_qty=1.0,
        wallet_btc=0.0,
        reserved_btc=0.00731916,
    )
    assert ok is True

    ok, why = never_sell_treasury_allowed(
        product="BTC-USD",
        sell_qty=0.001,
        wallet_btc=None,
        reserved_btc=0.00731916,
        never_sell_treasury=True,
    )
    assert ok is False and why == "never_sell_treasury_no_balance"


def test_settings_defaults_keep_nsr_and_treasury_hooks() -> None:
    s = Settings(
        _env_file=None,
        stock_enabled=False,
        futures_enabled=False,
        crash_enabled=False,
        fed_enabled=False,
    )
    assert s.never_sell_red is True
    assert s.never_sell_red_emergency is True
    assert s.treasury_reserve_enabled is True
    assert s.never_sell_treasury is True
