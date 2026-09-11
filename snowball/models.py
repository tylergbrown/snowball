from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Signal(StrEnum):
    ENTER = "enter"
    EXIT = "exit"
    HOLD = "hold"


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class Ticker:
    product: str
    last: float | None
    bid: float | None
    ask: float | None
    ts: datetime

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return None

    @property
    def reference(self) -> float | None:
        if self.last is not None:
            return self.last
        return self.mid


@dataclass
class Position:
    id: int
    product: str
    side: str
    qty: float
    entry_price: float
    notional_usd: float
    opened_at: datetime
    status: str
    closed_at: datetime | None = None
    exit_price: float | None = None
    realized_pnl: float | None = None
    strategy: str = "sma_15m"


@dataclass
class Fill:
    id: int
    position_id: int | None
    product: str
    side: str
    qty: float
    price: float
    notional_usd: float
    fee_usd: float
    slippage_bps: float
    ts: datetime
    reason: str
    strategy: str = "sma_15m"


@dataclass
class PairSnapshot:
    product: str
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    sma_fast: float | None = None
    sma_slow: float | None = None
    signal: str = Signal.HOLD.value
    candle_ts: datetime | None = None
    sma_fast_5m: float | None = None
    sma_slow_5m: float | None = None
    signal_5m: str = Signal.HOLD.value
    candle_ts_5m: datetime | None = None
    sma_fast_1d: float | None = None
    sma_slow_1d: float | None = None
    signal_1d: str = Signal.HOLD.value
    candle_ts_1d: datetime | None = None
    ema_fast_15m: float | None = None
    ema_slow_15m: float | None = None
    signal_ema_15m: str = Signal.HOLD.value
    donchian_high_1d: float | None = None
    donchian_low_1d: float | None = None
    signal_donchian_1d: str = Signal.HOLD.value
    # RSI (Wilder) + Bollinger per timeframe (filters + rsi_*/bb_* strategies)
    rsi_15m: float | None = None
    rsi_5m: float | None = None
    rsi_1d: float | None = None
    bb_upper_15m: float | None = None
    bb_mid_15m: float | None = None
    bb_lower_15m: float | None = None
    bb_upper_5m: float | None = None
    bb_mid_5m: float | None = None
    bb_lower_5m: float | None = None
    bb_upper_1d: float | None = None
    bb_mid_1d: float | None = None
    bb_lower_1d: float | None = None
    signal_rsi_15m: str = Signal.HOLD.value
    signal_rsi_1d: str = Signal.HOLD.value
    signal_bb_15m: str = Signal.HOLD.value
    signal_bb_1d: str = Signal.HOLD.value
    # CFM stock lane: last 15m window looks stalled (set by stock engine)
    stalled_15m: bool = False
    open_count: int = 0
    max_open: int = 2
    last_error: str | None = None


@dataclass
class RiskSnapshot:
    trading_enabled: bool
    halt_active: bool
    daily_killed: bool
    mode: str
    live_enabled: bool
    bankroll_usd: float
    cash_usd: float
    equity_usd: float
    daily_pnl_usd: float
    daily_loss_kill_usd: float
    start_of_day_equity: float
    open_positions: int
    max_positions_per_pair: int
    max_position_notional_usd: float
    block_reasons: list[str] = field(default_factory=list)
