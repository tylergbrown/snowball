"""Offline backtest with CSV fixture — no ledger writes."""

from __future__ import annotations

from pathlib import Path

from snowball.backtest import (
    load_ohlcv_csv,
    main,
    run_backtest,
    simulate_atr_trail_candidate,
    simulate_sma_with_gates,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ohlcv_sample.csv"


def test_load_fixture() -> None:
    data = load_ohlcv_csv(FIXTURE)
    assert len(data) >= 60
    assert len(data.close) == len(data.high) == len(data.low)


def test_sma_and_atr_summaries_offline() -> None:
    data = load_ohlcv_csv(FIXTURE)
    summaries = run_backtest(data)
    names = {s["name"] for s in summaries}
    assert "sma_gates" in names
    assert "atr_trail_candidate" in names
    for s in summaries:
        assert "closed_trades" in s
        assert "realized_pnl" in s
        assert "win_rate" in s


def test_backtest_does_not_touch_ledger(tmp_path: Path, monkeypatch) -> None:
    """Running backtest must not create/write a snowball.db under cwd."""
    monkeypatch.chdir(tmp_path)
    data = load_ohlcv_csv(FIXTURE)
    simulate_sma_with_gates(data)
    simulate_atr_trail_candidate(data)
    assert not (tmp_path / "data").exists()
    assert list(tmp_path.glob("**/*.db")) == []


def test_cli_csv(capsys) -> None:
    rc = main(["--csv", str(FIXTURE)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sma_gates" in out
    assert "atr_trail_candidate" in out
