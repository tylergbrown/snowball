"""Bitcoin treasury ledger + PDF metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from snowball.treasury.ledger import (
    SEED_POLICY,
    ensure_schema,
    holdings_and_cost,
    record_contribution,
    record_mark,
    snapshot,
    trailing_7d_weekly_pnl,
)


def test_seed_policy_does_not_claim_crypto_fuel() -> None:
    assert SEED_POLICY["seeded_as_treasury"] is False
    assert float(SEED_POLICY["crypto_fuel_deposit_usd"]) == 620.0


def test_empty_snapshot_graceful(tmp_path: Path) -> None:
    db = tmp_path / "snowball_treasury.db"
    ensure_schema(db)
    now = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)
    snap = snapshot(
        db,
        now=now,
        spot_now=100_000.0,
        spot_at=lambda _t: 90_000.0,
        write_mark=True,
    )
    assert snap.empty
    assert snap.avg_price_usd is None
    assert snap.total_pnl_usd is None
    assert snap.holdings_btc == 0.0
    assert snap.trend_30d_btc_spot_pct is not None
    assert abs(snap.trend_30d_btc_spot_pct - (100_000 - 90_000) / 90_000 * 100) < 1e-6
    assert snap.trend_30d_label == "30-day BTC-USD spot"
    assert snap.weekly_pnl_usd == 0.0


def test_avg_price_and_total_pnl(tmp_path: Path) -> None:
    db = tmp_path / "snowball_treasury.db"
    t0 = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
    record_contribution(
        db,
        usd_amount=50.0,
        btc_qty=0.0005,
        btc_price=100_000.0,
        kind="friday_50",
        ts=t0,
        note="weekly friday",
    )
    record_contribution(
        db,
        usd_amount=50.0,
        btc_qty=0.0004,
        btc_price=125_000.0,
        kind="pnl_sweep",
        ts=t0 + timedelta(days=1),
    )
    qty, cost, n = holdings_and_cost(db)
    assert n == 2
    assert abs(qty - 0.0009) < 1e-12
    assert abs(cost - 100.0) < 1e-9
    now = t0 + timedelta(days=10)
    snap = snapshot(
        db,
        now=now,
        spot_now=120_000.0,
        spot_at=lambda _t: 110_000.0,
        write_mark=True,
    )
    assert not snap.empty
    assert snap.avg_price_usd is not None
    assert abs(snap.avg_price_usd - (100.0 / 0.0009)) < 1e-6
    assert snap.total_pnl_usd is not None
    assert abs(snap.total_pnl_usd - (0.0009 * 120_000.0 - 100.0)) < 1e-6


def test_trailing_7d_weekly_pnl_from_marks(tmp_path: Path) -> None:
    db = tmp_path / "snowball_treasury.db"
    t0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    record_contribution(
        db,
        usd_amount=100.0,
        btc_qty=0.001,
        btc_price=100_000.0,
        kind="manual",
        ts=t0,
    )
    # Mark at t0: value 100, cost 100, pnl 0
    record_mark(db, btc_usd=100_000.0, ts=t0)
    now = t0 + timedelta(days=7)
    # Mark now: value 110, cost 100, pnl +10 → weekly +10
    weekly = trailing_7d_weekly_pnl(
        db, now=now, mark_value_now=110.0, cost_now=100.0
    )
    assert weekly is not None
    assert abs(weekly - 10.0) < 1e-6


def test_schema_writes_seed_policy_meta(tmp_path: Path) -> None:
    import json
    import sqlite3

    db = tmp_path / "snowball_treasury.db"
    ensure_schema(db)
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT value FROM meta WHERE key='seed_policy'"
    ).fetchone()
    con.close()
    assert row is not None
    policy = json.loads(row[0])
    assert policy["seeded_as_treasury"] is False
