"""Fed Desk: prob bucket math, 70% gate, direction mapping, allocation 33/32/20/10/5."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from snowball.allocation import lane_budget_pcts, lane_budgets_usd
from snowball.config import LiveTradingRefused, Settings
from snowball.fed.probs import (
    BET_SKEW_THRESHOLD,
    classify_meeting_probs,
    in_fomc_bet_window,
    summarize_fedwatch_payload,
)


TARGET = "3.50%-3.75%"


def test_prob_bucket_math_hold_hike_cut() -> None:
    # Current live-like: 44% hold / 56% hike
    s = classify_meeting_probs(
        {"3.50%-3.75%": 44.0, "3.75%-4.00%": 56.0},
        TARGET,
    )
    assert s.p_hold == pytest.approx(0.44)
    assert s.p_hike == pytest.approx(0.56)
    assert s.p_cut == pytest.approx(0.0)
    assert s.dominant == "hike"
    assert s.max_prob == pytest.approx(0.56)

    cut_dom = classify_meeting_probs(
        {"3.25%-3.50%": 80.0, "3.50%-3.75%": 20.0},
        TARGET,
    )
    assert cut_dom.dominant == "cut"
    assert cut_dom.p_cut == pytest.approx(0.80)
    assert cut_dom.p_hold == pytest.approx(0.20)

    hold_dom = classify_meeting_probs(
        {"3.50%-3.75%": 90.0, "3.75%-4.00%": 10.0},
        TARGET,
    )
    assert hold_dom.dominant == "hold"
    assert hold_dom.p_hold == pytest.approx(0.90)


def test_no_entry_below_70_percent() -> None:
    s = classify_meeting_probs(
        {"3.50%-3.75%": 44.0, "3.75%-4.00%": 56.0},
        TARGET,
    )
    assert s.max_prob < BET_SKEW_THRESHOLD
    assert s.bet_eligible is False
    assert s.direction is None

    # Even clear hike but under 70 stays flat
    s2 = classify_meeting_probs(
        {"3.50%-3.75%": 35.0, "3.75%-4.00%": 65.0},
        TARGET,
    )
    assert s2.dominant == "hike"
    assert s2.bet_eligible is False


def test_entry_direction_cut_long_hike_short() -> None:
    hike = classify_meeting_probs(
        {"3.50%-3.75%": 20.0, "3.75%-4.00%": 80.0},
        TARGET,
    )
    assert hike.bet_eligible is True
    assert hike.direction == "short"
    assert hike.dominant == "hike"

    cut = classify_meeting_probs(
        {"3.25%-3.50%": 75.0, "3.50%-3.75%": 25.0},
        TARGET,
    )
    assert cut.bet_eligible is True
    assert cut.direction == "long"
    assert cut.dominant == "cut"

    # Hold >=70% → flat in v1
    hold = classify_meeting_probs(
        {"3.50%-3.75%": 85.0, "3.75%-4.00%": 15.0},
        TARGET,
    )
    assert hold.dominant == "hold"
    assert hold.bet_eligible is False
    assert hold.direction is None


def test_fomc_window_and_summarize() -> None:
    today = date(2026, 9, 10)
    assert in_fomc_bet_window("2026-09-16", today=today) is True
    assert in_fomc_bet_window("2026-10-28", today=today) is False
    assert in_fomc_bet_window((today - timedelta(days=1)).isoformat(), today=today) is False

    payload = {
        "effr": 3.63,
        "current_target": TARGET,
        "meetings": [
            {
                "date": "2026-09-16",
                "contract": "ZQU6",
                "probabilities": {"3.50%-3.75%": 44.0, "3.75%-4.00%": 56.0},
            }
        ],
    }
    summary = summarize_fedwatch_payload(payload)
    assert summary["next_meeting_date"] == "2026-09-16"
    assert summary["p_hold"] == pytest.approx(0.44)
    assert summary["p_hike"] == pytest.approx(0.56)
    assert summary["bet_eligible"] is False  # below 70 even inside window
    assert summary["in_window"] is True


def test_allocation_40_25_30_10_5() -> None:
    pcts = lane_budget_pcts()
    assert pcts["crypto"] == 0.40
    assert pcts["stock"] == 0.25
    assert pcts["futures"] == 0.30
    assert pcts["crash"] == 0.10
    assert pcts["fed"] == 0.05
    assert pcts["spot"] == 0.65
    assert pcts["crypto_stock_shared"] == 1.0
    lane_sum = pcts["crypto"] + pcts["stock"] + pcts["futures"] + pcts["crash"] + pcts["fed"]
    assert lane_sum == pytest.approx(1.10)  # FT 30%; spot share is crypto+stock
    budgets = lane_budgets_usd(10_000.0)
    assert budgets["fed_usd"] == 500.0
    s = Settings(
        _env_file=None,
        stock_enabled=False,
        futures_enabled=False,
        crash_enabled=False,
        fed_enabled=False,
    )
    assert s.lane_budget_pcts()["fed"] == 0.05


def test_fed_live_dual_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FED_MODE", raising=False)
    monkeypatch.delenv("FED_LIVE_ENABLED", raising=False)
    s = Settings(
        _env_file=None,
        fed_enabled=True,
        fed_mode="live",
        fed_live_enabled=False,
        fed_sqlite_path=tmp_path / "fed.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
        futures_enabled=False,
        crash_enabled=False,
    )
    with pytest.raises(LiveTradingRefused):
        s.assert_fed_config()
    assert s.fed_live_orders_permitted() is False

    ok = Settings(
        _env_file=None,
        fed_enabled=True,
        fed_mode="live",
        fed_live_enabled=True,
        fed_sqlite_path=tmp_path / "fed2.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c2.db",
        heartbeat_path=tmp_path / "hb2",
        stock_enabled=False,
        futures_enabled=False,
        crash_enabled=False,
    )
    ok.assert_fed_config()
    assert ok.fed_live_orders_permitted() is True
