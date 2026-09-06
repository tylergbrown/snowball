"""Scorecard aggregation + snapshot exposure."""

from __future__ import annotations

from datetime import datetime, timezone

from snowball.paper import PaperLedger
from snowball.snapshot import build_snapshot
from snowball.state import AppState


def test_scorecard_by_strategy_and_product(ledger: PaperLedger) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    p15, _ = ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "b", ts=now, strategy="sma_15m"
    )
    ledger.close_position(p15.id, 110.0, 0.0, 0.0, "win", ts=now)
    p5, _ = ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "b", ts=now, strategy="sma_5m"
    )
    ledger.close_position(p5.id, 90.0, 0.0, 0.0, "loss", ts=now)
    ledger.open_buy("SOL-USD", 50.0, 100.0, 0.0, 0.0, "open", ts=now, strategy="sma_15m")

    sc = ledger.scorecard()
    by_s = {r["strategy"]: r for r in sc["by_strategy"]}
    assert by_s["sma_15m"]["closed_trades"] == 1
    assert by_s["sma_15m"]["wins"] == 1
    assert by_s["sma_15m"]["win_rate"] == 1.0
    assert by_s["sma_15m"]["realized_pnl"] == 10.0
    assert by_s["sma_15m"]["open_count"] == 1
    assert by_s["sma_5m"]["closed_trades"] == 1
    assert by_s["sma_5m"]["wins"] == 0
    assert by_s["sma_5m"]["win_rate"] == 0.0
    assert by_s["sma_5m"]["realized_pnl"] == -10.0

    by_p = {r["product"]: r for r in sc["by_product"]}
    assert by_p["BTC-USD"]["closed_trades"] == 2
    assert by_p["BTC-USD"]["wins"] == 1
    assert by_p["BTC-USD"]["win_rate"] == 0.5
    assert by_p["SOL-USD"]["open_count"] == 1
    assert by_p["SOL-USD"]["closed_trades"] == 0


def test_snapshot_includes_scorecard_and_pauses(app_state: AppState, monkeypatch) -> None:
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("snowball.snapshot.utcnow", lambda: now)
    for _ in range(3):
        pos, _ = app_state.ledger.open_buy("DOGE-USD", 1.0, 10.0, 0.0, 0.0, "x", ts=now)
        app_state.ledger.close_position(pos.id, 0.5, 0.0, 0.0, "loss", ts=now)
        app_state.ledger.record_closed_trade_for_pause(
            "DOGE-USD", -5.0, now=now, enabled=True, loss_threshold=3, pause_hours=24
        )
    snap = build_snapshot(app_state)
    assert "scorecard" in snap
    assert "by_strategy" in snap["scorecard"]
    assert "by_product" in snap["scorecard"]
    assert any(p["product"] == "DOGE-USD" and p["active"] for p in snap["pair_pauses"])
    doge = next(p for p in snap["pairs"] if p["product"] == "DOGE-USD")
    assert doge["paused"] is True
    assert doge["paused_until"] is not None
