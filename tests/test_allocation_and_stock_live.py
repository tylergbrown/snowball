"""Capital split 40/15 shared-spot 55 + FT40/Crash/Fed, fee-buffer exits, stock dual-gate + CFM."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from snowball.allocation import (
    effective_take_profit_floor,
    lane_budget_pcts,
    lane_budgets_usd,
    leg_notional_usd,
    remaining_budget_usd,
    spot_open_notional_usd,
    spot_shared_budget_usd,
)
from snowball.config import LiveTradingRefused, Settings
from snowball.gates import strategy_exit_allowed
from snowball.models import Position, PositionStatus, Ticker
from snowball.paper import PaperLedger
from snowball.state import AppState
from snowball.stocks.engine import StockPaperEngine, attach_stock_lane
from snowball.stocks.market import resolve_coinbase_equity_perps, ticker_to_perp_product
from snowball.futures.market import normalize_futures_product
from snowball.models import PairSnapshot


def test_allocation_helpers_40_15_40_10_5_shared() -> None:
    pcts = lane_budget_pcts()
    assert pcts == {
        "crypto": 0.40,
        "stock": 0.15,
        "futures": 0.40,
        "crash": 0.10,
        "fed": 0.05,
        "spot": 0.55,
        "crypto_stock_shared": 1.0,
    }
    budgets = lane_budgets_usd(1000.0)
    assert budgets["crypto_usd"] == 400.0
    assert budgets["stock_usd"] == 150.0
    assert budgets["spot_usd"] == 550.0
    assert budgets["futures_usd"] == 400.0
    assert budgets["crash_usd"] == 100.0
    assert budgets["fed_usd"] == 50.0
    s = Settings(_env_file=None, stock_enabled=False, futures_enabled=False, crash_enabled=False, fed_enabled=False)
    assert s.crypto_account_budget_pct == 0.40
    assert s.stock_account_budget_pct == 0.15
    assert s.crypto_stock_shared_budget is True
    assert s.futures_account_budget_pct == 0.40
    assert s.crash_account_budget_pct == 0.10
    assert s.fed_account_budget_pct == 0.05
    assert s.lane_budget_pcts() == pcts
    assert s.min_take_profit_pct == 0.06
    assert s.fee_buffer_pct == 0.01
    assert s.effective_min_take_profit_pct() == pytest.approx(0.07)


def test_spot_shared_vs_separate_remaining_budget() -> None:
    """Shared pool lets either lane use idle capital up to 55%; separate keeps lanes apart."""
    av = 10_000.0
    crypto_pct, stock_pct = 0.40, 0.15
    shared_budget = spot_shared_budget_usd(av, crypto_pct, stock_pct)
    assert shared_budget == 5500.0

    class _Lot:
        def __init__(self, n: float) -> None:
            self.notional_usd = n

    crypto_lots = [_Lot(1000.0)]
    stock_lots = [_Lot(500.0)]
    open_shared = spot_open_notional_usd(crypto_lots, stock_lots)
    assert open_shared == 1500.0
    # Crypto can still deploy: shared remaining 4000 even though crypto-alone
    # remaining would be 3000 if capped at 40% with only crypto open counted.
    assert remaining_budget_usd(shared_budget, open_shared) == 4000.0
    crypto_only_budget = av * crypto_pct
    crypto_only_open = 1000.0
    assert remaining_budget_usd(crypto_only_budget, crypto_only_open) == 3000.0
    # Idle stock capital (1500 - 500 = 1000) is available to crypto under sharing:
    assert remaining_budget_usd(shared_budget, open_shared) == (
        remaining_budget_usd(crypto_only_budget, crypto_only_open) + 1000.0
    )

    # Separate mode: stock remaining ignores crypto open
    stock_only_budget = av * stock_pct
    assert remaining_budget_usd(stock_only_budget, 500.0) == 1000.0
    # Shared with crypto full of its 40% still has stock slice usable:
    heavy_crypto = [_Lot(4000.0)]
    assert remaining_budget_usd(
        shared_budget, spot_open_notional_usd(heavy_crypto, stock_lots)
    ) == 1000.0

    s_off = Settings(
        _env_file=None,
        stock_enabled=False,
        futures_enabled=False,
        crash_enabled=False,
        fed_enabled=False,
        crypto_stock_shared_budget=False,
    )
    pcts_off = s_off.lane_budget_pcts()
    assert pcts_off["crypto_stock_shared"] == 0.0
    assert pcts_off["spot"] == 0.55


def test_fee_buffer_math() -> None:
    assert effective_take_profit_floor(0.06, 0.01) == pytest.approx(0.07)
    lot = Position(
        id=1,
        product="BTC-USD",
        side="long",
        qty=1.0,
        entry_price=100.0,
        notional_usd=100.0,
        opened_at=datetime.now(timezone.utc),
        status=PositionStatus.OPEN,
        strategy="sma_15m",
    )
    # Never exit below entry
    ok, reason = strategy_exit_allowed(
        lot, 99.0, min_take_profit_pct=0.06, never_sell_red=True, fee_buffer_pct=0.01
    )
    assert ok is False and reason == "never_sell_red"
    # 6% green refused after fee buffer (needs 7%)
    ok, reason = strategy_exit_allowed(
        lot, 106.0, min_take_profit_pct=0.06, never_sell_red=True, fee_buffer_pct=0.01
    )
    assert ok is False and reason == "below_take_profit"
    # 7% ok
    ok, reason = strategy_exit_allowed(
        lot, 107.0, min_take_profit_pct=0.06, never_sell_red=True, fee_buffer_pct=0.01
    )
    assert ok is True and reason == "ok"


def test_never_exit_below_entry() -> None:
    lot = Position(
        id=2,
        product="AAPL",
        side="long",
        qty=1.0,
        entry_price=200.0,
        notional_usd=200.0,
        opened_at=datetime.now(timezone.utc),
        status=PositionStatus.OPEN,
        strategy="sma_15m",
    )
    ok, reason = strategy_exit_allowed(
        lot, 200.0, min_take_profit_pct=0.0, never_sell_red=True, fee_buffer_pct=0.0
    )
    # pnl_pct == 0 is not < 0, so never_sell_red passes; below_take_profit if min>0
    assert ok is True
    ok, reason = strategy_exit_allowed(
        lot, 199.99, min_take_profit_pct=0.0, never_sell_red=True, fee_buffer_pct=0.0
    )
    assert ok is False and reason == "never_sell_red"


def test_stock_live_refused_without_both_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STOCK_MODE", raising=False)
    monkeypatch.delenv("STOCK_LIVE_ENABLED", raising=False)
    s_live_only = Settings(
        _env_file=None,
        stock_enabled=True,
        stock_mode="live",
        stock_live_enabled=False,
        stock_sqlite_path=tmp_path / "s.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c.db",
        heartbeat_path=tmp_path / "hb",
    )
    with pytest.raises(LiveTradingRefused):
        s_live_only.assert_stock_config()
    assert s_live_only.stock_live_orders_permitted() is False

    s_flag_only = Settings(
        _env_file=None,
        stock_enabled=True,
        stock_mode="paper",
        stock_live_enabled=True,
        stock_sqlite_path=tmp_path / "s2.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c2.db",
        heartbeat_path=tmp_path / "hb2",
    )
    with pytest.raises(LiveTradingRefused):
        s_flag_only.assert_stock_config()

    s_both = Settings(
        _env_file=None,
        stock_enabled=True,
        stock_mode="live",
        stock_live_enabled=True,
        stock_sqlite_path=tmp_path / "s3.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c3.db",
        heartbeat_path=tmp_path / "hb3",
        coinbase_api_key="organizations/x/apiKeys/y",
        coinbase_api_secret="-----BEGIN EC PRIVATE KEY-----\nM\n-----END EC PRIVATE KEY-----\n",
    )
    s_both.assert_stock_config()
    assert s_both.stock_live_orders_permitted() is True


def test_resolve_intx_perps_maps_and_skips() -> None:
    markets = {
        "AAPL/USDC:USDC": {
            "id": "AAPL-PERP-INTX",
            "base": "AAPL",
            "type": "swap",
        },
        "MSFT/USDC:USDC": {
            "id": "MSFT-PERP-INTX",
            "base": "MSFT",
            "type": "swap",
        },
        "BTC/USDC:USDC": {
            "id": "BTC-PERP-INTX",
            "base": "BTC",
            "type": "swap",
        },
    }
    mapped = resolve_coinbase_equity_perps(
        ["AAPL", "MSFT", "NFLX", "SPY"],
        markets=markets,
        exclude_bases={"SPY", "QQQ"},
    )
    assert mapped == {"AAPL": "AAPL-PERP-INTX", "MSFT": "MSFT-PERP-INTX"}
    assert "NFLX" not in mapped
    assert "SPY" not in mapped
    assert ticker_to_perp_product("AAPL") == "AAPL-PERP-INTX"


class FakeYahoo:
    mark_source = "yahoo_paper"

    def __init__(self) -> None:
        self.closes = [100.0] * 50 + [200.0]
        self.orders: list[tuple] = []

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        closes = self.closes[-limit:]
        step = {"5m": 300_000.0, "15m": 900_000.0, "1d": 86_400_000.0}.get(timeframe, 900_000.0)
        base = 1_700_000_000_000.0
        return [[base + i * step, c, c, c, c, 1.0] for i, c in enumerate(closes)]

    def fetch_ticker(self, product: str) -> Ticker:
        return Ticker(
            product=product,
            last=200.0,
            bid=None,
            ask=None,
            ts=datetime.now(timezone.utc),
        )


class FakeIntxMarket:
    mark_source = "coinbase_perp"

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.allow_orders = True

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        closes = [100.0] * 50 + [200.0]
        step = 900_000.0
        base = 1_700_000_000_000.0
        return [[base + i * step, c, c, c, c, 1.0] for i, c in enumerate(closes[-limit:])]

    def fetch_ticker(self, product: str) -> Ticker:
        return Ticker(
            product=product,
            last=200.0,
            bid=199.0,
            ask=201.0,
            ts=datetime.now(timezone.utc),
        )

    def fetch_account_value_usd(self, *, crypto_marks=None) -> float:
        return 1000.0

    def fetch_bba(self, product):
        return 199.0, 201.0

    def create_swap_market_order(self, product, side, amount, *, leverage=1.0, reduce_only=False):
        assert product.endswith("-CDE") or product.endswith("-PERP-INTX") or "PERP" in product
        assert "yahoo" not in str(product).lower()
        order = {
            "id": f"ord-{len(self.orders)+1}",
            "filled": float(amount),
            "average": 200.0,
            "price": 200.0,
            "fee": {"cost": 0.1, "currency": "USDC"},
        }
        self.orders.append(
            {
                "product": product,
                "side": side,
                "amount": amount,
                "reduce_only": reduce_only,
                "type": "market",
            }
        )
        return order

    def create_swap_maker_limit_order(
        self,
        product,
        side,
        amount,
        *,
        price=None,
        bid=None,
        ask=None,
        leverage=1.0,
        reduce_only=False,
        timeout_sec=None,
        post_only=True,
    ):
        assert product.endswith("-CDE") or product.endswith("-PERP-INTX") or "PERP" in product
        assert price is not None and float(price) > 0
        px = float(price)
        order = {
            "id": f"ord-{len(self.orders)+1}",
            "filled": float(amount),
            "average": px,
            "price": px,
            "remaining": 0.0,
            "status": "closed",
            "fee": {"cost": 0.1, "currency": "USDC"},
        }
        self.orders.append(
            {
                "product": product,
                "side": side,
                "amount": amount,
                "reduce_only": reduce_only,
                "type": "limit",
                "price": px,
            }
        )
        return order


def test_stock_live_uses_cfm_not_intx_for_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STOCK_MODE", raising=False)
    monkeypatch.delenv("STOCK_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    settings = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "crypto.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=True,
        stock_mode="live",
        stock_live_enabled=True,
        stock_sqlite_path=tmp_path / "stocks.db",
        stock_strategies="sma_15m",
        stock_products="US5-19DEC30-CDE,TEK-19DEC30-CDE",
        stock_max_positions=1,
        stock_max_notional_usd=4000.0,
        indicator_filters_enabled=False,
        stock_max_active=8,
        stock_dynamic_max=0,
        stock_account_budget_pct=0.40,
        stock_bankroll_usd=5000.0,
        futures_enabled=False,
        crash_enabled=False,
        fed_enabled=False,
        cfm_max_contracts=1,
        cfm_leverage=1.0,
        cfm_margin_rate=0.10,
        min_take_profit_pct=0.06,
        fee_buffer_pct=0.01,
        never_sell_red=True,
    )
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    settings.assert_stock_config()
    state.stock_ledger = PaperLedger(settings.stock_sqlite_path, settings.stock_bankroll_usd)
    state.stock_universe_active = ["US5-19DEC30-CDE", "TEK-19DEC30-CDE"]
    for p in state.stock_universe_active:
        state.stock_pairs[p] = PairSnapshot(product=p, max_open=1)
    engine = StockPaperEngine(state, market=FakeYahoo())
    fake_cb = FakeIntxMarket()
    engine._cb_market = fake_cb
    engine._perp_map = {
        "US5-19DEC30-CDE": "US5-19DEC30-CDE",
        "TEK-19DEC30-CDE": "TEK-19DEC30-CDE",
    }
    state.stock_coinbase_ids = dict(engine._perp_map)
    engine._last_budget = {
        "account_value_usd": 5000.0,
        "budget_usd": 2000.0,
        "open_notional_usd": 0.0,
    }
    import time as _time
    engine._last_universe_refresh = _time.monotonic()

    # Seed marks + SMA so strategy can enter
    for prod, px in (("US5-19DEC30-CDE", 3061.0), ("TEK-19DEC30-CDE", 2200.0)):
        snap = state.stock_pairs[prod]
        snap.last = px
        snap.bid = px - 0.5
        snap.ask = px + 0.5
        snap.sma_fast = px + 1
        snap.sma_slow = px - 1
        snap.signal = "enter"

    # Direct live open (bypass full strategy path flakiness)
    engine._open_lot_live(
        product="US5-19DEC30-CDE",
        reason="sma_15m:enter",
        now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        strategy="sma_15m",
        notional_usd=118.0,  # deliberate tiny soft-cap; must still size 1 CFM
    )
    assert state.stock_ledger.open_count("US5-19DEC30-CDE") == 1
    assert fake_cb.orders, "expected CFM swap order"
    assert fake_cb.orders[0]["product"] == "US5-19DEC30-CDE"
    assert fake_cb.orders[0]["side"] == "buy"
    assert float(fake_cb.orders[0]["amount"]) == 1.0
    lot = state.stock_ledger.open_positions("US5-19DEC30-CDE")[0]
    assert engine._lot_is_live_backed(lot) is True


def test_stock_live_skips_when_crash_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowball.crash.store import CrashStore

    monkeypatch.delenv("STOCK_MODE", raising=False)
    monkeypatch.delenv("STOCK_LIVE_ENABLED", raising=False)
    settings = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "crypto.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=True,
        stock_mode="live",
        stock_live_enabled=True,
        stock_sqlite_path=tmp_path / "stocks.db",
        stock_products="US5-19DEC30-CDE,TEK-19DEC30-CDE",
        stock_max_notional_usd=4000.0,
        stock_bankroll_usd=5000.0,
        futures_enabled=False,
        crash_enabled=True,
        fed_enabled=False,
        cfm_max_contracts=1,
        indicator_filters_enabled=False,
    )
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    state.stock_ledger = PaperLedger(settings.stock_sqlite_path, settings.stock_bankroll_usd)
    state.crash_ledger = CrashStore(tmp_path / "crash.db", 10_000.0)
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    state.crash_ledger.open_short(
        product="US5-19DEC30-CDE",
        fill_px=3061.0,
        notional_usd=3061.0,
        slippage_bps=0.0,
        fee_usd=0.0,
        reason="test",
        ts=now,
        strategy="crash_guard",
    )
    engine = StockPaperEngine(state, market=FakeYahoo())
    fake_cb = FakeIntxMarket()
    engine._cb_market = fake_cb
    engine._perp_map = {"US5-19DEC30-CDE": "US5-19DEC30-CDE"}
    engine._last_budget = {
        "account_value_usd": 5000.0,
        "budget_usd": 2000.0,
        "open_notional_usd": 0.0,
    }
    state.stock_pairs["US5-19DEC30-CDE"] = PairSnapshot(
        product="US5-19DEC30-CDE", max_open=1, last=3061.0, bid=3060.0, ask=3062.0
    )
    engine._open_lot_live(
        product="US5-19DEC30-CDE",
        reason="sma_15m:enter",
        now=now,
        strategy="sma_15m",
        notional_usd=4000.0,
    )
    assert state.stock_ledger.open_count("US5-19DEC30-CDE") == 0
    assert fake_cb.orders == []


def test_leg_notional_respects_budget() -> None:
    assert leg_notional_usd(
        budget_usd=400.0, open_notional_usd=350.0, max_notional_usd=100.0, target_legs=8
    ) == pytest.approx(50.0)
    assert leg_notional_usd(
        budget_usd=400.0, open_notional_usd=0.0, max_notional_usd=100.0, target_legs=8
    ) == pytest.approx(100.0)
    assert (
        leg_notional_usd(
            budget_usd=400.0, open_notional_usd=400.0, max_notional_usd=100.0, target_legs=8
        )
        == 0.0
    )


def test_sma_min_take_profit_floor() -> None:
    """SMA strategies need 9% effective (8% + 1% fee); non-SMA stay at 7%."""
    from snowball.maker import min_take_profit_for_strategy

    s = Settings(_env_file=None, stock_enabled=False, futures_enabled=False)
    assert s.sma_min_take_profit_pct == pytest.approx(0.08)
    assert s.min_take_profit_pct_for("sma_5m") == pytest.approx(0.08)
    assert s.min_take_profit_pct_for("sma_15m") == pytest.approx(0.08)
    assert s.min_take_profit_pct_for("sma_1d") == pytest.approx(0.08)
    assert s.min_take_profit_pct_for("ema_15m") == pytest.approx(0.06)
    assert s.effective_min_take_profit_pct_for("sma_15m") == pytest.approx(0.09)
    assert s.effective_min_take_profit_pct_for("ema_15m") == pytest.approx(0.07)
    assert min_take_profit_for_strategy(
        "donchian_1d", min_take_profit_pct=0.06, sma_min_take_profit_pct=0.08
    ) == pytest.approx(0.06)

    lot_sma = Position(
        id=10,
        product="BTC-USD",
        side="long",
        qty=1.0,
        entry_price=100.0,
        notional_usd=100.0,
        opened_at=datetime.now(timezone.utc),
        status=PositionStatus.OPEN,
        strategy="sma_5m",
    )
    # 7% green refused for SMA (needs 9%)
    ok, reason = strategy_exit_allowed(
        lot_sma,
        107.0,
        min_take_profit_pct=s.min_take_profit_pct_for(lot_sma.strategy),
        never_sell_red=True,
        fee_buffer_pct=0.01,
    )
    assert ok is False and reason == "below_take_profit"
    # 9% allowed
    ok, reason = strategy_exit_allowed(
        lot_sma,
        109.0,
        min_take_profit_pct=s.min_take_profit_pct_for(lot_sma.strategy),
        never_sell_red=True,
        fee_buffer_pct=0.01,
    )
    assert ok is True and reason == "ok"

    lot_other = Position(
        id=11,
        product="BTC-USD",
        side="long",
        qty=1.0,
        entry_price=100.0,
        notional_usd=100.0,
        opened_at=datetime.now(timezone.utc),
        status=PositionStatus.OPEN,
        strategy="ema_15m",
    )
    ok, reason = strategy_exit_allowed(
        lot_other,
        107.0,
        min_take_profit_pct=s.min_take_profit_pct_for(lot_other.strategy),
        never_sell_red=True,
        fee_buffer_pct=0.01,
    )
    assert ok is True and reason == "ok"
    # Never sell below entry
    ok, reason = strategy_exit_allowed(
        lot_sma,
        99.0,
        min_take_profit_pct=s.min_take_profit_pct_for(lot_sma.strategy),
        never_sell_red=True,
        fee_buffer_pct=0.01,
    )
    assert ok is False and reason == "never_sell_red"


def test_maker_buy_price_rests_at_or_inside_bid() -> None:
    from snowball.maker import maker_buy_price, maker_sell_price

    assert maker_buy_price(100.0, 101.0) is not None
    px = maker_buy_price(100.0, 101.0)
    assert px is not None and 100.0 <= px < 101.0
    assert maker_buy_price(None, 101.0) is None
    sp = maker_sell_price(100.0, 101.0)
    assert sp is not None and 100.0 < sp <= 101.0
