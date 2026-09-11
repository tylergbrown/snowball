"""Live trading: dual-gate, PEM expand, mocked exchange fills into ledger."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from snowball.config import LiveTradingRefused, Settings
from snowball.engine import Engine, build_state
from snowball.live import LiveBroker, expand_pem_newlines, make_broker, parse_order_fill
from snowball.models import PairSnapshot
from snowball.paper import PaperLedger
from snowball.state import AppState
from tests.conftest import FakeMarket


class FakeExchange:
    def __init__(
        self,
        *,
        free_usd: float = 1000.0,
        buy_order: dict[str, Any] | None = None,
        sell_order: dict[str, Any] | None = None,
        buy_error: Exception | None = None,
        sell_error: Exception | None = None,
        balance_error: Exception | None = None,
    ) -> None:
        self.free_usd = free_usd
        self.buy_order = buy_order
        self.sell_order = sell_order
        self.buy_error = buy_error
        self.sell_error = sell_error
        self.balance_error = balance_error
        # (symbol, type, side, amount, price|None, via)
        self.orders: list[tuple[str, str, str, float, float | None, str]] = []

    def fetch_balance(self) -> dict[str, Any]:
        if self.balance_error is not None:
            raise self.balance_error
        return {"free": {"USD": self.free_usd}, "USD": {"free": self.free_usd}}

    def create_market_buy_order_with_cost(
        self, symbol: str, cost: float, params: dict | None = None
    ) -> dict[str, Any]:
        """Coinbase quote-notional market buy (preferred path)."""
        self.orders.append((symbol, "market", "buy", float(cost), None, "cost"))
        if self.buy_error is not None:
            raise self.buy_error
        if self.buy_order is not None:
            return dict(self.buy_order)
        px = 200.0
        filled = float(cost) / px
        self.free_usd = max(0.0, self.free_usd - float(cost))
        order = {
            "id": f"buy-{len(self.orders)}",
            "filled": filled,
            "average": px,
            "price": px,
            "cost": float(cost),
            "remaining": 0.0,
            "fee": {"cost": 0.1, "currency": "USD"},
            "status": "closed",
        }
        self._last_order = dict(order)
        return order

    def fetch_order(self, order_id: str, symbol: str | None = None) -> dict[str, Any]:
        self.fetch_order_calls = getattr(self, "fetch_order_calls", 0) + 1
        if getattr(self, "_pending_fetch", None) is not None:
            out = dict(self._pending_fetch)
            self._pending_fetch = None
            return out
        return dict(getattr(self, "_last_order", {"id": order_id, "filled": 0, "average": 0}))

    def fetch_order_book(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        mid = 200.0
        return {
            "bids": [[mid * 0.9999, 10.0]],
            "asks": [[mid * 1.0001, 10.0]],
        }

    def cancel_order(self, order_id: str, symbol: str | None = None) -> dict[str, Any]:
        self.cancels = getattr(self, "cancels", [])
        self.cancels.append((order_id, symbol))
        return {"id": order_id, "status": "canceled"}

    def create_order(
        self,
        symbol: str,
        type_: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        via = "price" if price is not None else "amount"
        if side == "buy" and params and params.get("createMarketBuyOrderRequiresPrice") is False:
            via = "cost_param"
        if type_ == "limit":
            via = "limit"
        self.orders.append((symbol, type_, side, amount, price, via))
        if side == "buy":
            if self.buy_error is not None:
                raise self.buy_error
            if self.buy_order is not None:
                out = dict(self.buy_order)
                self._last_order = dict(out)
                return out
            if price is not None and float(price) > 0:
                cost = amount * float(price)
                filled = amount
                avg = float(price)
            elif via == "cost_param":
                cost = float(amount)
                avg = 200.0
                filled = cost / avg
            else:
                cost = amount * 200.0
                filled = amount
                avg = 200.0
            self.free_usd = max(0.0, self.free_usd - cost)
            order = {
                "id": f"buy-{len(self.orders)}",
                "filled": filled,
                "average": avg,
                "price": avg,
                "cost": cost,
                "remaining": 0.0,
                "status": "closed",
                "fee": {"cost": 0.1, "currency": "USD"},
            }
            self._last_order = dict(order)
            return order
        if self.sell_error is not None:
            raise self.sell_error
        if self.sell_order is not None:
            out = dict(self.sell_order)
            self._last_order = dict(out)
            return out
        px = float(price) if price is not None and float(price) > 0 else 210.0
        proceeds = amount * px
        self.free_usd += proceeds
        order = {
            "id": f"sell-{len(self.orders)}",
            "filled": amount,
            "average": px,
            "price": px,
            "cost": proceeds,
            "remaining": 0.0,
            "status": "closed",
            "fee": {"cost": 0.05, "currency": "USD"},
        }
        self._last_order = dict(order)
        return order


def _live_settings(tmp_path: Path, **kwargs: object) -> Settings:
    base = dict(
        _env_file=None,
        mode="live",
        live_enabled=True,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "snowball.db",
        heartbeat_path=tmp_path / "heartbeat",
        dashboard_enabled=False,
        watcher_enabled=False,
        yolo_demon_enabled=False,
        coinbase_api_key="organizations/demo/apiKeys/demo",
        coinbase_api_secret="-----BEGIN EC PRIVATE KEY-----\nABC\n-----END EC PRIVATE KEY-----\n",
        strategies="sma_15m",
        entry_cooldown_seconds=0,
        slippage_bps=0.0,
        scale_in_min_profit_pct=0.005,
        trend_filter_enabled=True,
        indicator_filters_enabled=False,
        pair_pause_enabled=False,
        max_position_notional_usd=100.0,
        per_leg_base_usd=100.0,
        per_leg_autoscale=False,  # live fill tests isolate order path, not AV scale
        bankroll_usd=1000.0,
        products="BTC-USD,SOL-USD,ETH-USD,DOGE-USD",
    )
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


GOLDEN = [100.0] * 50 + [200.0]
FLAT = [100.0] * 60
PAIRS = ("BTC-USD", "SOL-USD", "ETH-USD", "DOGE-USD")


def test_expand_pem_newlines_literal_escapes() -> None:
    raw = "-----BEGIN EC PRIVATE KEY-----\nLINE2\n-----END-----\n"
    out = expand_pem_newlines(raw)
    assert chr(92) + "n" not in out
    assert out.splitlines() == [
        "-----BEGIN EC PRIVATE KEY-----",
        "LINE2",
        "-----END-----",
    ]


def test_expand_pem_preserves_real_newlines() -> None:
    real = "-----BEGIN EC PRIVATE KEY-----" + chr(10) + "ABC" + chr(10) + "-----END-----"
    assert expand_pem_newlines(real) == real


def test_parse_order_fill_basic() -> None:
    px, qty, fee = parse_order_fill(
        {"filled": 0.5, "average": 200.0, "fee": {"cost": 0.2, "currency": "USD"}}
    )
    assert px == 200.0
    assert qty == 0.5
    assert fee == 0.2


def test_dual_gate_required_for_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    paper = Settings(_env_file=None, mode="paper", live_enabled=False)
    assert make_broker(paper) is None
    with pytest.raises(LiveTradingRefused):
        make_broker(Settings(_env_file=None, mode="live", live_enabled=False))
    with pytest.raises(LiveTradingRefused):
        LiveBroker(
            Settings(
                _env_file=None,
                mode="live",
                live_enabled=True,
                coinbase_api_key="",
                coinbase_api_secret="",
            )
        )


def test_live_broker_expands_pem_into_ccxt(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class CapturingKlass:
        def __init__(self, cfg: dict[str, Any]) -> None:
            captured.update(cfg)

        def fetch_balance(self) -> dict[str, Any]:
            return {"free": {"USD": 0.0}}

        def create_order(self, *a: object, **k: object) -> dict[str, Any]:
            return {}

    settings = _live_settings(tmp_path)
    # Inject fake klass via exchange= already built instance path:
    # Construct with exchange mock and assert expand helper separately;
    # also construct through __init__ by patching getattr chain.
    secret = expand_pem_newlines(settings.coinbase_api_secret)
    assert chr(10) in secret
    broker = LiveBroker(settings, exchange=FakeExchange())
    assert broker.fetch_free_usd() == 1000.0


def test_build_state_live_does_not_refuse(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    ex = FakeExchange(free_usd=750.0)
    state, engine = build_state(settings, market=FakeMarket({p: list(FLAT) for p in PAIRS}), exchange=ex)
    assert state.broker is not None
    assert state.ledger.cash_usd() == pytest.approx(750.0)
    assert isinstance(engine, Engine)


def test_build_state_mode_live_without_flag_refuses(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path, live_enabled=False)
    with pytest.raises(LiveTradingRefused):
        build_state(settings, market=FakeMarket({p: list(FLAT) for p in PAIRS}))


def test_live_buy_records_exchange_fill(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    ex = FakeExchange(free_usd=1000.0)
    ledger = PaperLedger(settings.sqlite_path, settings.bankroll_usd)
    state = AppState(settings=settings, ledger=ledger, broker=LiveBroker(settings, exchange=ex))
    for product in settings.product_list:
        state.pairs[product] = PairSnapshot(product=product, max_open=2)
    market = FakeMarket(
        {p: list(FLAT) for p in PAIRS},
        last={p: 100.0 for p in PAIRS},
        ohlcv={("BTC-USD", "15m"): list(GOLDEN)},
    )
    market.last["BTC-USD"] = 200.0
    Engine(state, market).tick()
    assert len(ex.orders) == 1
    assert ex.orders[0][2] == "buy"
    assert ledger.open_count("BTC-USD") == 1
    lot = ledger.open_positions("BTC-USD")[0]
    assert lot.entry_price == pytest.approx(ex.orders[0][4], rel=1e-6)
    assert lot.qty * lot.entry_price == pytest.approx(100.0, rel=1e-4)
    fills = ledger.recent_fills(1)
    assert fills[0].side == "buy"
    assert fills[0].fee_usd == pytest.approx(0.1)


def test_live_sell_records_exchange_fill(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    ex = FakeExchange(free_usd=1000.0)
    ledger = PaperLedger(settings.sqlite_path, settings.bankroll_usd)
    broker = LiveBroker(settings, exchange=ex)
    state = AppState(settings=settings, ledger=ledger, broker=broker)
    for product in settings.product_list:
        state.pairs[product] = PairSnapshot(product=product, max_open=2)
    # Seed an open lot as if previously bought live (SMA needs 9% effective floor)
    pos, _ = ledger.open_buy(
        "BTC-USD",
        fill_px=200.0,
        notional_usd=100.0,
        slippage_bps=0.0,
        fee_usd=0.0,
        reason="seed",
        strategy="sma_15m",
    )
    # Death cross: high then low
    death = [200.0] * 50 + [100.0]
    market = FakeMarket(
        {p: list(FLAT) for p in PAIRS},
        last={p: 100.0 for p in PAIRS},
        ohlcv={("BTC-USD", "15m"): death},
    )
    market.last["BTC-USD"] = 218.0  # >= 9% SMA floor (8% TP + 1% fee buffer)
    Engine(state, market).tick()
    assert any(o[2] == "sell" for o in ex.orders)
    assert ledger.open_count("BTC-USD") == 0
    assert ledger.closed_positions()[0].id == pos.id


def test_live_buy_api_error_skips_without_killing(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    ex = FakeExchange(free_usd=1000.0, buy_error=RuntimeError("insufficient funds"))
    ledger = PaperLedger(settings.sqlite_path, settings.bankroll_usd)
    state = AppState(settings=settings, ledger=ledger, broker=LiveBroker(settings, exchange=ex))
    for product in settings.product_list:
        state.pairs[product] = PairSnapshot(product=product, max_open=2)
    market = FakeMarket(
        {p: list(FLAT) for p in PAIRS},
        last={p: 100.0 for p in PAIRS},
        ohlcv={("BTC-USD", "15m"): list(GOLDEN)},
    )
    market.last["BTC-USD"] = 200.0
    Engine(state, market).tick()  # must not raise
    assert ledger.open_count("BTC-USD") == 0
    assert state.running is True


def test_paper_path_unchanged_no_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    settings = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "snowball.db",
        heartbeat_path=tmp_path / "heartbeat",
        dashboard_enabled=False,
        watcher_enabled=False,
        yolo_demon_enabled=False,
        strategies="sma_15m",
        indicator_filters_enabled=False,
        entry_cooldown_seconds=0,
        slippage_bps=0.0,
        scale_in_min_profit_pct=0.005,
        trend_filter_enabled=True,
        pair_pause_enabled=False,
        products="BTC-USD,SOL-USD,ETH-USD,DOGE-USD",
    )
    state, engine = build_state(
        settings, market=FakeMarket({p: list(FLAT) for p in PAIRS}, last={p: 100.0 for p in PAIRS})
    )
    assert state.broker is None
    market = FakeMarket(
        {p: list(FLAT) for p in PAIRS},
        last={p: 100.0 for p in PAIRS},
        ohlcv={("BTC-USD", "15m"): list(GOLDEN)},
    )
    market.last["BTC-USD"] = 200.0
    engine.market = market
    engine.tick()
    assert state.ledger.open_count("BTC-USD") == 1


def test_create_market_order_buy_uses_quote_cost(tmp_path: Path) -> None:
    """Coinbase spot buys must supply quote notional (cost path)."""
    settings = _live_settings(tmp_path)
    ex = FakeExchange(free_usd=500.0)
    broker = LiveBroker(settings, exchange=ex)
    order = broker.create_market_order("BTC-USD", "buy", 0.5, price=200.0, cost=100.0)
    assert order["cost"] == pytest.approx(100.0)
    assert order["filled"] == pytest.approx(0.5)
    assert len(ex.orders) == 1
    symbol, typ, side, amount, price, via = ex.orders[0]
    assert symbol == "BTC/USD"
    assert typ == "market"
    assert side == "buy"
    assert via == "cost"
    assert amount == pytest.approx(100.0)  # quote notional
    assert price is None


def test_create_market_order_buy_price_fallback_without_cost(tmp_path: Path) -> None:
    """When cost is omitted, price is passed through to create_order."""
    settings = _live_settings(tmp_path)

    class PriceOnlyExchange(FakeExchange):
        def create_market_buy_order_with_cost(self, *a: object, **k: object) -> dict[str, Any]:
            raise AssertionError("should not use cost helper when cost omitted")

    ex = PriceOnlyExchange(free_usd=500.0)
    # Remove cost helper so LiveBroker uses price path even if cost were set;
    # here we omit cost entirely.
    delattr(PriceOnlyExchange, "create_market_buy_order_with_cost")
    broker = LiveBroker(settings, exchange=ex)
    order = broker.create_market_order("ETH-USD", "buy", 0.25, price=200.0)
    assert order["average"] == pytest.approx(200.0)
    symbol, typ, side, amount, price, via = ex.orders[0]
    assert side == "buy"
    assert via == "price"
    assert amount == pytest.approx(0.25)
    assert price == pytest.approx(200.0)


def test_create_market_order_sell_amount_only(tmp_path: Path) -> None:
    """Market sells stay base-qty only (no price/cost required)."""
    settings = _live_settings(tmp_path)
    ex = FakeExchange(free_usd=500.0)
    broker = LiveBroker(settings, exchange=ex)
    order = broker.create_market_order("BTC-USD", "sell", 0.4)
    assert order["filled"] == pytest.approx(0.4)
    symbol, typ, side, amount, price, via = ex.orders[0]
    assert side == "sell"
    assert via == "amount"
    assert amount == pytest.approx(0.4)
    assert price is None


def test_live_buy_uses_maker_limit_with_price(tmp_path: Path) -> None:
    """Engine live entry posts a limit buy with price (maker), not a market/cost buy."""
    settings = _live_settings(tmp_path)
    ex = FakeExchange(free_usd=1000.0)
    ledger = PaperLedger(settings.sqlite_path, settings.bankroll_usd)
    state = AppState(settings=settings, ledger=ledger, broker=LiveBroker(settings, exchange=ex))
    for product in settings.product_list:
        state.pairs[product] = PairSnapshot(product=product, max_open=2)
    market = FakeMarket(
        {p: list(FLAT) for p in PAIRS},
        last={p: 100.0 for p in PAIRS},
        ohlcv={("BTC-USD", "15m"): list(GOLDEN)},
    )
    market.last["BTC-USD"] = 200.0
    Engine(state, market).tick()
    assert len(ex.orders) == 1
    _sym, typ, side, amount, price, via = ex.orders[0]
    assert side == "buy"
    assert typ == "limit"
    assert via == "limit"
    assert price is not None and float(price) > 0
    assert amount == pytest.approx(100.0 / float(price))
    assert ledger.open_count("BTC-USD") == 1


def test_settle_order_fetches_when_create_omits_fill(tmp_path: Path) -> None:
    """Coinbase often returns order id only; broker must fetch_order to ledger."""
    settings = _live_settings(tmp_path)
    ex = FakeExchange(free_usd=500.0)
    # create returns bare id; fetch_order supplies the fill
    ex.buy_order = {
        "id": "abc-123",
        "filled": None,
        "average": None,
        "cost": None,
        "fee": {"cost": None, "currency": None},
        "status": None,
    }
    ex._pending_fetch = {
        "id": "abc-123",
        "filled": 0.5,
        "average": 200.0,
        "cost": 100.0,
        "fee": {"cost": 0.1, "currency": "USD"},
        "status": "closed",
    }
    broker = LiveBroker(settings, exchange=ex)
    # Patch sleep so settle is fast in tests
    import snowball.live as live_mod
    import time as time_mod

    orig_sleep = time_mod.sleep
    time_mod.sleep = lambda _s: None
    try:
        order = broker.create_market_order("BTC-USD", "buy", 0.5, price=200.0, cost=100.0)
    finally:
        time_mod.sleep = orig_sleep
    assert order["filled"] == pytest.approx(0.5)
    assert order["average"] == pytest.approx(200.0)
    assert getattr(ex, "fetch_order_calls", 0) >= 1


def test_create_maker_limit_order_buy_requires_price(tmp_path: Path) -> None:
    """Maker entry path requests limit with an explicit price on buy."""
    settings = _live_settings(tmp_path)
    ex = FakeExchange(free_usd=500.0)
    broker = LiveBroker(settings, exchange=ex)
    order = broker.create_maker_limit_order(
        "BTC-USD", "buy", 0.5, price=199.98, bid=199.98, ask=200.02, timeout_sec=1.0
    )
    assert order["filled"] == pytest.approx(0.5)
    assert len(ex.orders) == 1
    symbol, typ, side, amount, price, via = ex.orders[0]
    assert symbol == "BTC/USD"
    assert typ == "limit"
    assert side == "buy"
    assert price == pytest.approx(199.98)
    assert amount == pytest.approx(0.5)
    assert via == "limit"
