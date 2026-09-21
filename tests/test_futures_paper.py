"""Future Trader — session day-trade + dual-gated live; paper path in tests."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from snowball.config import LiveTradingRefused, Settings
from snowball.dashboard import create_app
from snowball.futures.engine import FuturesPaperEngine, attach_futures_lane
from snowball.futures.market import (
    DEFAULT_FUTURES_PRODUCTS,
    estimate_account_value_usd,
    normalize_futures_product,
    to_futures_ccxt_symbol,
)
from snowball.futures.session import (
    classify_session_state,
    entry_allowed,
    in_exit_window,
)
from snowball.gates import strategy_exit_allowed
from snowball.models import PairSnapshot, Position, Signal, Ticker
from snowball.paper import PaperLedger
from snowball.snapshot import build_snapshot
from snowball.state import AppState
from snowball.strategy import crossover_signal, donchian_breakout_signal

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_product_symbol_mapping() -> None:
    from snowball.futures.market import is_cfm_product

    assert normalize_futures_product("spy-perp-intx") == "US5-19DEC30-CDE"
    assert normalize_futures_product("QQQ") == "TEK-19DEC30-CDE"
    assert normalize_futures_product("US5-19DEC30-CDE") == "US5-19DEC30-CDE"
    assert normalize_futures_product("TEK") == "TEK-19DEC30-CDE"
    assert to_futures_ccxt_symbol("US5-19DEC30-CDE") == "CDEUS5/USD:USD-301219"
    assert to_futures_ccxt_symbol("TEK-19DEC30-CDE") == "CDETEK/USD:USD-301219"
    # Stock-lane INTX single names still map to USDC swaps
    assert to_futures_ccxt_symbol("AAPL-PERP-INTX") == "AAPL/USDC:USDC"
    assert to_futures_ccxt_symbol("NVDA") == "NVDA/USDC:USDC"
    assert is_cfm_product("US5-19DEC30-CDE")
    assert is_cfm_product("SPY")
    assert not is_cfm_product("AAPL-PERP-INTX")
    assert DEFAULT_FUTURES_PRODUCTS == ("US5-19DEC30-CDE", "TEK-19DEC30-CDE")


def test_sma_1d_signal_on_synthetic_daily() -> None:
    closes = [100.0] * 50 + [200.0]
    assert crossover_signal(closes, 20, 50) is Signal.ENTER


def test_donchian_1d_signal_on_synthetic_daily() -> None:
    n = 22
    highs = [10.0] * (n - 1) + [100.0]
    lows = [10.0] * n
    closes = [10.0] * (n - 1) + [11.0]
    assert donchian_breakout_signal(closes, highs, lows) is Signal.ENTER


def test_live_gate_requires_both_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    monkeypatch.delenv("FUTURES_LIVE_ENABLED", raising=False)
    s_live_only = Settings(
        _env_file=None,
        futures_enabled=True,
        futures_mode="live",
        futures_live_enabled=False,
        futures_sqlite_path=tmp_path / "f.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
    )
    with pytest.raises(LiveTradingRefused):
        s_live_only.assert_futures_config()
    assert s_live_only.futures_live_orders_permitted() is False

    s_flag_only = Settings(
        _env_file=None,
        futures_enabled=True,
        futures_mode="paper",
        futures_live_enabled=True,
        futures_sqlite_path=tmp_path / "f2.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c2.db",
        heartbeat_path=tmp_path / "hb2",
        stock_enabled=False,
    )
    with pytest.raises(LiveTradingRefused):
        s_flag_only.assert_futures_config()

    s_both = Settings(
        _env_file=None,
        futures_enabled=True,
        futures_mode="live",
        futures_live_enabled=True,
        futures_sqlite_path=tmp_path / "f3.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c3.db",
        heartbeat_path=tmp_path / "hb3",
        stock_enabled=False,
    )
    s_both.assert_futures_config()
    assert s_both.futures_live_orders_permitted() is True


def test_never_sell_red_blocks_red_exit() -> None:
    lot = Position(
        id=1,
        product="US5-19DEC30-CDE",
        side="long",
        qty=0.1,
        entry_price=100.0,
        notional_usd=100.0,
        opened_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        status="open",
        strategy="session_day",
    )
    ok, reason = strategy_exit_allowed(
        lot, mark=95.0, min_take_profit_pct=0.05, never_sell_red=True
    )
    assert ok is False
    assert reason == "never_sell_red"


def test_session_windows() -> None:
    # Thursday 2026-09-10
    assert entry_allowed(_et(2026, 9, 10, 9, 25))
    assert entry_allowed(_et(2026, 9, 10, 10, 0))  # late catch-up
    assert not entry_allowed(_et(2026, 9, 10, 9, 0))
    assert not entry_allowed(_et(2026, 9, 10, 15, 55))  # exit window
    assert in_exit_window(_et(2026, 9, 10, 15, 55))
    assert not in_exit_window(_et(2026, 9, 10, 15, 0))
    assert not entry_allowed(_et(2026, 9, 12, 9, 26))  # Saturday


def test_budget_split_50_50() -> None:
    bal = {"free": {"USD": 4000.0, "USDC": 6000.0}, "used": {"USD": 0.0}, "total": {}}
    acct = estimate_account_value_usd(bal)
    assert acct == 10000.0
    budget = acct * 0.10
    assert budget == 1000.0
    assert budget / 2 == 500.0


class FakeFuturesMarket:
    mark_source = "coinbase_perp"

    def __init__(self, last: float = 200.0) -> None:
        self.last = last
        self.orders: list[dict] = []
        self.account_value = 10_000.0
        self._allow_orders = False

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        closes = [100.0] * 50 + [self.last]
        step = 86_400_000.0
        base = 1_700_000_000_000.0
        return [[base + i * step, c, c + 1, c - 1, c, 1.0] for i, c in enumerate(closes[-limit:])]

    def fetch_ticker(self, product: str) -> Ticker:
        return Ticker(
            product=product,
            last=self.last,
            bid=self.last - 0.1,
            ask=self.last + 0.1,
            ts=datetime.now(timezone.utc),
        )

    def fetch_account_value_usd(self, *, crypto_marks=None) -> float:
        return float(self.account_value)

    def fetch_bba(self, product):
        return self.last - 0.1, self.last + 0.1

    def create_swap_market_order(self, product, side, amount, *, leverage=1.0, reduce_only=False):
        if not self._allow_orders:
            raise RuntimeError("orders not allowed")
        order = {
            "id": f"ord-{len(self.orders)+1}",
            "filled": float(amount),
            "average": float(self.last),
            "price": float(self.last),
            "cost": float(amount) * float(self.last),
            "fee": {"cost": 0.0, "currency": "USDC"},
            "status": "closed",
        }
        self.orders.append({"product": product, "side": side, "amount": amount, "type": "market", **order})
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
        if not self._allow_orders:
            raise RuntimeError("orders not allowed")
        px = float(price) if price is not None else float(self.last)
        order = {
            "id": f"ord-{len(self.orders)+1}",
            "filled": float(amount),
            "average": px,
            "price": px,
            "cost": float(amount) * px,
            "remaining": 0.0,
            "fee": {"cost": 0.0, "currency": "USDC"},
            "status": "closed",
        }
        self.orders.append(
            {
                "product": product,
                "side": side,
                "amount": amount,
                "type": "limit",
                "limit_price": px,
                **order,
            }
        )
        return order


def _paper_settings(tmp_path: Path, **kwargs) -> Settings:
    base = dict(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "crypto.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
        futures_enabled=True,
        futures_mode="paper",
        futures_live_enabled=False,
        futures_sqlite_path=tmp_path / "futures.db",
        futures_strategies="session_day",
        futures_products="US5-19DEC30-CDE,TEK-19DEC30-CDE",
        futures_poll_seconds=0.05,
        futures_max_positions=1,
        futures_max_notional_usd=500.0,
        futures_account_budget_pct=0.10,
        futures_bankroll_usd=1000.0,
        never_sell_red=True,
        min_take_profit_pct=0.05,
        sma_min_take_profit_pct=0.05,
        slippage_bps=0.0,
        pair_pause_enabled=False,
    )
    base.update(kwargs)
    return Settings(**base)


def test_session_entry_and_no_second_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    settings = _paper_settings(tmp_path)
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_futures_lane(state)
    market = FakeFuturesMarket(last=200.0)
    engine = FuturesPaperEngine(state, market=market)  # type: ignore[arg-type]

    entry_now = _et(2026, 9, 10, 9, 26).astimezone(timezone.utc)
    # Force budget
    engine._last_budget = {
        "account_value_usd": 1000.0,
        "budget_usd": 100.0,
        "per_index_usd": 50.0,
    }
    # Manually drive session act with frozen budget refresh bypass via tick pieces
    for product in ("US5-19DEC30-CDE", "TEK-19DEC30-CDE"):
        engine._update_pair(product)
        marks = {product: 200.0 for product in ("US5-19DEC30-CDE", "TEK-19DEC30-CDE")}
        engine._refresh_budget(marks)
        engine._act_session(
            product=product,
            now=entry_now,
            halted=False,
            can_trade=True,
            daily_killed=False,
            marks=marks,
        )
    assert state.futures_ledger.open_count("US5-19DEC30-CDE") == 1
    assert state.futures_ledger.open_count("TEK-19DEC30-CDE") == 1

    # Second entry same day while open — blocked
    engine._act_session(
        product="US5-19DEC30-CDE",
        now=entry_now + timedelta(minutes=2),
        halted=False,
        can_trade=True,
        daily_killed=False,
        marks={"US5-19DEC30-CDE": 200.0, "TEK-19DEC30-CDE": 200.0},
    )
    assert state.futures_ledger.open_count("US5-19DEC30-CDE") == 1

    # 50/50-ish notionals (paper uses per_index from bankroll*10%/2)
    spy = state.futures_ledger.open_positions("US5-19DEC30-CDE")[0]
    qqq = state.futures_ledger.open_positions("TEK-19DEC30-CDE")[0]
    assert spy.notional_usd == pytest.approx(qqq.notional_usd, rel=0.05)


def test_no_entry_when_holding_overnight_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    settings = _paper_settings(tmp_path)
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_futures_lane(state)
    assert state.futures_ledger is not None
    # Seed overnight loser opened prior ET day
    prior = _et(2026, 9, 9, 9, 26)
    state.futures_ledger.open_buy(
        product="US5-19DEC30-CDE",
        fill_px=200.0,
        notional_usd=50.0,
        slippage_bps=0.0,
        fee_usd=0.0,
        reason="session_day:session_enter",
        ts=prior.astimezone(timezone.utc),
        strategy="session_day",
    )
    market = FakeFuturesMarket(last=190.0)  # still red
    engine = FuturesPaperEngine(state, market=market)  # type: ignore[arg-type]
    state.futures_pairs["US5-19DEC30-CDE"] = PairSnapshot(
        product="US5-19DEC30-CDE", last=190.0, max_open=1
    )
    next_morning = _et(2026, 9, 10, 9, 26).astimezone(timezone.utc)
    lots = state.futures_ledger.open_positions("US5-19DEC30-CDE")
    assert classify_session_state(open_lots=lots, now=next_morning) == "holding_overnight"
    engine._act_session(
        product="US5-19DEC30-CDE",
        now=next_morning,
        halted=False,
        can_trade=True,
        daily_killed=False,
        marks={"US5-19DEC30-CDE": 190.0},
    )
    assert state.futures_ledger.open_count("US5-19DEC30-CDE") == 1  # no second lot


def test_close_only_if_green_at_close_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    settings = _paper_settings(tmp_path)
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_futures_lane(state)
    assert state.futures_ledger is not None
    opened = _et(2026, 9, 10, 9, 26).astimezone(timezone.utc)
    state.futures_ledger.open_buy(
        product="US5-19DEC30-CDE",
        fill_px=200.0,
        notional_usd=50.0,
        slippage_bps=0.0,
        fee_usd=0.0,
        reason="session_day:session_enter",
        ts=opened,
        strategy="session_day",
    )
    market = FakeFuturesMarket(last=195.0)
    engine = FuturesPaperEngine(state, market=market)  # type: ignore[arg-type]
    state.futures_pairs["US5-19DEC30-CDE"] = PairSnapshot(
        product="US5-19DEC30-CDE", last=195.0, bid=194.9, ask=195.1, max_open=1
    )
    close_now = _et(2026, 9, 10, 15, 56).astimezone(timezone.utc)
    # Red → hold
    engine._act_session(
        product="US5-19DEC30-CDE",
        now=close_now,
        halted=False,
        can_trade=True,
        daily_killed=False,
        marks={"US5-19DEC30-CDE": 195.0},
    )
    assert state.futures_ledger.open_count("US5-19DEC30-CDE") == 1

    # Green → close
    market.last = 201.0
    state.futures_pairs["US5-19DEC30-CDE"].last = 201.0
    engine._act_session(
        product="US5-19DEC30-CDE",
        now=close_now,
        halted=False,
        can_trade=True,
        daily_killed=False,
        marks={"US5-19DEC30-CDE": 201.0},
    )
    assert state.futures_ledger.open_count("US5-19DEC30-CDE") == 0


def test_paper_still_works_and_api_shows_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    settings = _paper_settings(tmp_path)
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_futures_lane(state)
    from fastapi.testclient import TestClient

    # Populate budget fields as tick would
    state.futures_account_value_usd = 10000.0
    state.futures_budget_usd = 1000.0
    state.futures_per_index_allotment_usd = 500.0
    state.futures_session_states = {
        "US5-19DEC30-CDE": "flat",
        "TEK-19DEC30-CDE": "flat",
    }

    client = TestClient(create_app(state))
    r = client.get("/api/futures")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["mode"] == "paper"
    assert body["risk"]["budget_pct"] == 0.10
    assert body["risk"]["per_index_allotment_usd"] == 500.0
    assert body["session"]["entry_window_et"].startswith("09:25")
    assert "US5-19DEC30-CDE" in body["products"]
    page = client.get("/")
    assert b"Future Trader" in page.content
    snap = build_snapshot(state)
    assert snap["futures"]["enabled"] is True


def test_futures_engine_does_not_import_live_broker() -> None:
    root = Path(__file__).resolve().parents[1] / "snowball" / "futures"
    for py in root.glob("*.py"):
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "live" not in alias.name.split("."), py.name
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod != "snowball.live", py.name
                assert not mod.startswith("snowball.live."), py.name
                for alias in node.names:
                    assert alias.name != "LiveBroker", py.name


def test_open_lot_refuses_without_dual_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    settings = _paper_settings(tmp_path)
    # Flip only mode to live without live_enabled — open must refuse
    settings2 = settings.model_copy(
        update={"futures_mode": "live", "futures_live_enabled": False}
    )
    state = AppState(
        settings=settings2, ledger=PaperLedger(tmp_path / "c2.db", 1000.0)
    )
    state.futures_ledger = PaperLedger(tmp_path / "f2.db", 1000.0)
    state.futures_pairs["US5-19DEC30-CDE"] = PairSnapshot(
        product="US5-19DEC30-CDE", last=100.0, max_open=1
    )
    engine = FuturesPaperEngine(state, market=FakeFuturesMarket())  # type: ignore[arg-type]
    with pytest.raises(LiveTradingRefused):
        engine._open_lot(
            "US5-19DEC30-CDE",
            {"US5-19DEC30-CDE": 100.0},
            reason="session_day:session_enter",
            now=datetime.now(timezone.utc),
            strategy="session_day",
            notional_usd=50.0,
        )


def test_futures_defaults_session_day() -> None:
    s = Settings(_env_file=None)
    assert s.futures_strategy_list == ["session_day", "momentum_15m"]
    assert s.futures_uses_session_engine() is True
    assert s.futures_uses_momentum() is True
    assert s.futures_mode == "paper"
    assert s.futures_live_enabled is False
    assert s.futures_live_orders_permitted() is False
    assert s.futures_account_budget_pct == 0.40
    assert s.futures_max_positions == 1


def test_swap_order_leverage_is_string_for_coinbase() -> None:
    """Coinbase INTX rejects float leverage (proto string field); params must be str."""
    from snowball.futures.market import CoinbaseFuturesMarket, leverage_param

    assert leverage_param(1.0) == "1"
    assert leverage_param(1) == "1"
    assert leverage_param("2.0") == "2"
    assert isinstance(leverage_param(1.0), str)

    captured: list[dict] = []

    class Ex:
        def create_order(self, symbol, typ, side, amount, price, params):
            captured.append(dict(params or {}))
            return {
                "id": "t1",
                "filled": float(amount),
                "average": float(price or 100.0),
                "price": float(price or 100.0),
                "cost": float(amount) * float(price or 100.0),
                "remaining": 0.0,
                "fee": {"cost": 0.0, "currency": "USDC"},
                "status": "closed",
            }

        def fetch_order(self, order_id, symbol=None):
            return captured and {
                "id": order_id,
                "filled": 1.0,
                "average": 100.0,
                "price": 100.0,
                "status": "closed",
            }

    mkt = CoinbaseFuturesMarket(exchange=Ex(), allow_orders=True)
    mkt.create_swap_market_order("US5-19DEC30-CDE", "sell", 1.0, leverage=1.0)
    assert captured[-1]["leverage"] == "1"
    assert isinstance(captured[-1]["leverage"], str)

    mkt.create_swap_maker_limit_order(
        "GOOGL-PERP-INTX",
        "buy",
        1.0,
        price=100.0,
        bid=99.0,
        ask=101.0,
        leverage=1.0,
        timeout_sec=1.0,
        post_only=False,
    )
    assert captured[-1]["leverage"] == "1"
    assert isinstance(captured[-1]["leverage"], str)
    assert captured[-1].get("timeInForce") == "GTC"



def test_ft_invalid_post_only_falls_back_once_in_entry_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INVALID_LIMIT_PRICE_POST_ONLY during FT entry → one non-post-only limit fill."""
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    monkeypatch.delenv("FUTURES_LIVE_ENABLED", raising=False)

    class RejectThenFill(FakeFuturesMarket):
        def __init__(self) -> None:
            super().__init__(last=3947.0)
            self._allow_orders = True
            self.bba = (3946.0, 3947.0)

        def fetch_ticker(self, product: str) -> Ticker:
            bid, ask = self.bba
            return Ticker(
                product=product,
                last=3947.0,
                bid=bid,
                ask=ask,
                ts=datetime.now(timezone.utc),
            )

        def fetch_bba(self, product):
            return self.bba

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
            if post_only:
                self.orders.append(
                    {
                        "product": product,
                        "post_only": True,
                        "price": price,
                        "amount": amount,
                        "type": "limit",
                    }
                )
                raise RuntimeError(
                    'coinbase {"success":false, "error_response":'
                    '{"error":"INVALID_LIMIT_PRICE_POST_ONLY"}, '
                    '"order_configuration":{"limit_limit_gtc":'
                    '{"base_size":"1", "limit_price":"3947", "post_only":true}}}'
                )
            px = float(price if price is not None else (ask or self.last))
            order = {
                "id": "fallback-1",
                "filled": float(amount),
                "average": px,
                "price": px,
                "cost": float(amount) * px,
                "remaining": 0.0,
                "status": "closed",
                "fee": {"cost": 0.0, "currency": "USD"},
            }
            self.orders.append(
                {
                    "product": product,
                    "post_only": False,
                    "price": px,
                    "amount": amount,
                    "type": "limit",
                    **order,
                }
            )
            return order

    settings = _paper_settings(
        tmp_path,
        futures_mode="live",
        futures_live_enabled=True,
        futures_max_notional_usd=4000.0,
        coinbase_api_key="organizations/demo/apiKeys/demo",
        coinbase_api_secret="-----BEGIN EC PRIVATE KEY-----\nABC\n-----END EC PRIVATE KEY-----\n",
    )
    assert settings.cfm_max_contracts_per_index() == 1
    assert settings.cfm_order_leverage() == 1.0
    state = AppState(
        settings=settings, ledger=PaperLedger(tmp_path / "c-live.db", 10_000.0)
    )
    state.futures_ledger = PaperLedger(tmp_path / "f-live.db", 10_000.0)
    state.futures_pairs["TEK-19DEC30-CDE"] = PairSnapshot(
        product="TEK-19DEC30-CDE",
        last=3947.0,
        bid=3946.0,
        ask=3947.0,
        max_open=1,
    )
    mkt = RejectThenFill()
    engine = FuturesPaperEngine(state, market=mkt)  # type: ignore[arg-type]
    engine._last_budget = {
        "account_value_usd": 10_000.0,
        "budget_usd": 4000.0,
        "per_index_usd": 2000.0,
        "per_leg_notional_usd": 2000.0,
    }
    now = _et(2026, 9, 11, 9, 26).astimezone(timezone.utc)
    engine._open_lot(
        "TEK-19DEC30-CDE",
        {"TEK-19DEC30-CDE": 3947.0},
        reason="session_day:session_enter",
        now=now,
        strategy="session_day",
        notional_usd=2000.0,
    )
    assert state.futures_ledger is not None
    assert state.futures_ledger.open_count("TEK-19DEC30-CDE") == 1
    pos = state.futures_ledger.open_positions("TEK-19DEC30-CDE")[0]
    assert pos.qty == 1.0
    assert any(o.get("post_only") is True for o in mkt.orders)
    assert any(o.get("post_only") is False for o in mkt.orders)
    taker = [o for o in mkt.orders if o.get("post_only") is False][0]
    assert taker["amount"] == 1.0
    assert float(taker["price"]) == 3947.0  # ask touch, not a second bogus post-only


def test_ft_post_only_reject_no_fallback_outside_entry_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    monkeypatch.delenv("FUTURES_LIVE_ENABLED", raising=False)

    class AlwaysReject(FakeFuturesMarket):
        def __init__(self) -> None:
            super().__init__(last=3947.0)
            self._allow_orders = True

        def fetch_bba(self, product):
            return 3946.0, 3947.0

        def create_swap_maker_limit_order(self, *a, **k):
            self.orders.append({"post_only": k.get("post_only", True), "type": "limit"})
            raise RuntimeError("INVALID_LIMIT_PRICE_POST_ONLY")

        def create_swap_market_order(self, *a, **k):
            raise AssertionError("market fallback must not run outside entry window")

    settings = _paper_settings(
        tmp_path,
        futures_mode="live",
        futures_live_enabled=True,
        futures_max_notional_usd=4000.0,
        coinbase_api_key="organizations/demo/apiKeys/demo",
        coinbase_api_secret="-----BEGIN EC PRIVATE KEY-----\nABC\n-----END EC PRIVATE KEY-----\n",
    )
    state = AppState(
        settings=settings, ledger=PaperLedger(tmp_path / "c-out.db", 10_000.0)
    )
    state.futures_ledger = PaperLedger(tmp_path / "f-out.db", 10_000.0)
    state.futures_pairs["TEK-19DEC30-CDE"] = PairSnapshot(
        product="TEK-19DEC30-CDE", last=3947.0, bid=3946.0, ask=3947.0, max_open=1
    )
    mkt = AlwaysReject()
    engine = FuturesPaperEngine(state, market=mkt)  # type: ignore[arg-type]
    engine._last_budget = {
        "account_value_usd": 10_000.0,
        "budget_usd": 4000.0,
        "per_index_usd": 2000.0,
        "per_leg_notional_usd": 2000.0,
    }
    # Saturday — entry_allowed is False
    now = _et(2026, 9, 12, 9, 26).astimezone(timezone.utc)
    engine._open_lot(
        "TEK-19DEC30-CDE",
        {"TEK-19DEC30-CDE": 3947.0},
        reason="session_day:session_enter",
        now=now,
        strategy="session_day",
        notional_usd=2000.0,
    )
    assert state.futures_ledger is not None
    assert state.futures_ledger.open_count("TEK-19DEC30-CDE") == 0
    assert all(o.get("post_only") is True for o in mkt.orders)
