from pathlib import Path

import pytest

from snowball.market import fill_price
from snowball.models import Ticker
from snowball.paper import PaperLedger
from tests.conftest import make_ticker


def test_buy_uses_last_plus_slippage() -> None:
    ticker = make_ticker("BTC-USD", 100.0)
    px = fill_price(ticker, "buy", 5.0)
    assert px == pytest.approx(100.0 * 1.0005)


def test_sell_uses_last_minus_slippage() -> None:
    ticker = make_ticker("BTC-USD", 100.0)
    px = fill_price(ticker, "sell", 5.0)
    assert px == pytest.approx(100.0 * 0.9995)


def test_fill_price_falls_back_to_mid() -> None:
    ticker = Ticker(
        product="BTC-USD",
        last=None,
        bid=99.0,
        ask=101.0,
        ts=make_ticker("BTC-USD", 1).ts,
    )
    px = fill_price(ticker, "buy", 0.0)
    assert px == pytest.approx(100.0)


def test_buy_and_sell_update_cash_and_persist(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    ledger = PaperLedger(db, 1000.0)
    pos, fill = ledger.open_buy(
        "BTC-USD",
        fill_px=50_000.0,
        notional_usd=100.0,
        slippage_bps=5.0,
        fee_usd=0.0,
        reason="test",
    )
    assert fill.side == "buy"
    assert pos.qty == pytest.approx(100.0 / 50_000.0)
    assert ledger.cash_usd() == pytest.approx(900.0)
    assert ledger.open_count("BTC-USD") == 1

    sell = ledger.close_position(
        pos.id, fill_px=51_000.0, slippage_bps=5.0, fee_usd=0.0, reason="exit"
    )
    assert sell.side == "sell"
    assert ledger.cash_usd() == pytest.approx(1002.0)
    assert ledger.open_count("BTC-USD") == 0

    restarted = PaperLedger(db, 1000.0)
    assert restarted.cash_usd() == pytest.approx(1002.0)
    assert restarted.recent_fills(10)[0].side == "sell"
    assert restarted.open_positions() == []


def test_two_lots_same_pair(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path / "t.db", 1000.0)
    ledger.open_buy("ETH-USD", 2000.0, 100.0, 0.0, 0.0, "a")
    ledger.open_buy("ETH-USD", 2000.0, 100.0, 0.0, 0.0, "b")
    assert ledger.open_count("ETH-USD") == 2
    assert ledger.cash_usd() == pytest.approx(800.0)



def test_open_buy_tags_strategy(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path / "t.db", 1000.0)
    pos, fill = ledger.open_buy(
        "BTC-USD", 50_000.0, 100.0, 0.0, 0.0, "enter", strategy="sma_5m"
    )
    assert pos.strategy == "sma_5m"
    assert fill.strategy == "sma_5m"
    sell = ledger.close_position(pos.id, 51_000.0, 0.0, 0.0, "exit")
    assert sell.strategy == "sma_5m"


def test_last_entry_at_is_per_strategy(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    ledger = PaperLedger(tmp_path / "t.db", 1000.0)
    t0 = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    ledger.open_buy("BTC-USD", 100.0, 100.0, 0.0, 0.0, "a", ts=t0, strategy="sma_15m")
    later = t0 + timedelta(seconds=60)
    ledger.open_buy("BTC-USD", 100.0, 100.0, 0.0, 0.0, "b", ts=later, strategy="sma_5m")
    assert ledger.last_entry_at("BTC-USD", "sma_15m") == t0
    assert ledger.last_entry_at("BTC-USD", "sma_5m") == later


def test_migrates_strategy_column_on_old_db(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash_usd REAL NOT NULL,
            bankroll_usd REAL NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'long',
            qty REAL NOT NULL,
            entry_price REAL NOT NULL,
            notional_usd REAL NOT NULL,
            opened_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            closed_at TEXT,
            exit_price REAL,
            realized_pnl REAL
        );
        CREATE TABLE fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER,
            product TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL NOT NULL,
            notional_usd REAL NOT NULL,
            fee_usd REAL NOT NULL DEFAULT 0,
            slippage_bps REAL NOT NULL,
            ts TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE TABLE daily_state (
            utc_date TEXT PRIMARY KEY,
            start_equity REAL NOT NULL,
            killed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO account (id, cash_usd, bankroll_usd, updated_at)
        VALUES (1, 900.0, 1000.0, '2026-09-02T00:00:00+00:00');
        INSERT INTO positions
            (product, side, qty, entry_price, notional_usd, opened_at, status)
        VALUES ('BTC-USD', 'long', 0.002, 50000.0, 100.0, '2026-09-02T00:00:00+00:00', 'open');
        """
    )
    conn.commit()
    conn.close()

    ledger = PaperLedger(db, 1000.0)
    lots = ledger.open_positions("BTC-USD")
    assert len(lots) == 1
    assert lots[0].strategy == "sma_15m"
    assert ledger.cash_usd() == pytest.approx(900.0)
