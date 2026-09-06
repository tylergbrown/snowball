from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from snowball.config import Settings


@dataclass(frozen=True)
class RiskContext:
    now: datetime
    trading_enabled: bool
    halt_active: bool
    daily_killed: bool
    open_count_for_pair: int
    last_entry_at: datetime | None
    cash_usd: float
    requested_notional: float
    max_positions_per_pair: int
    max_position_notional_usd: float
    entry_cooldown: timedelta
    mode: str
    live_enabled: bool


def daily_loss_breached(equity: float, start_equity: float, kill_usd: float) -> bool:
    return (equity - start_equity) <= -abs(kill_usd)


def allow_entry(ctx: RiskContext) -> tuple[bool, str]:
    """Return (allowed, reason). reason is 'ok' on allow."""
    if ctx.mode == "live" and not ctx.live_enabled:
        return False, "live_not_enabled"
    if ctx.mode not in ("paper", "live"):
        return False, "bad_mode"
    if ctx.halt_active:
        return False, "halt_file"
    if not ctx.trading_enabled:
        return False, "trading_disabled"
    if ctx.daily_killed:
        return False, "daily_loss_kill"
    if ctx.open_count_for_pair >= ctx.max_positions_per_pair:
        return False, "max_positions_per_pair"
    if ctx.last_entry_at is not None and ctx.now - ctx.last_entry_at < ctx.entry_cooldown:
        return False, "cooldown"
    if ctx.requested_notional > ctx.max_position_notional_usd + 1e-9:
        return False, "notional_cap"
    if ctx.requested_notional <= 0:
        return False, "bad_notional"
    if ctx.cash_usd + 1e-9 < ctx.requested_notional:
        return False, "insufficient_cash"
    return True, "ok"


def allow_exit(ctx: RiskContext) -> tuple[bool, str]:
    """Exits are blocked by HALT / TRADING_ENABLED=false (zero orders).

    Daily-loss kill still allows flattening so the kill can actually stop bleeding.
    """
    if ctx.mode == "live" and not ctx.live_enabled:
        return False, "live_not_enabled"
    if ctx.halt_active:
        return False, "halt_file"
    if not ctx.trading_enabled:
        return False, "trading_disabled"
    return True, "ok"


def context_from_settings(
    settings: Settings,
    *,
    now: datetime,
    halt_active: bool,
    trading_enabled: bool,
    daily_killed: bool,
    open_count_for_pair: int,
    last_entry_at: datetime | None,
    cash_usd: float,
    requested_notional: float,
    cooldown_seconds: int | None = None,
) -> RiskContext:
    seconds = settings.entry_cooldown_seconds if cooldown_seconds is None else cooldown_seconds
    return RiskContext(
        now=now,
        trading_enabled=trading_enabled,
        halt_active=halt_active,
        daily_killed=daily_killed,
        open_count_for_pair=open_count_for_pair,
        last_entry_at=last_entry_at,
        cash_usd=cash_usd,
        requested_notional=requested_notional,
        max_positions_per_pair=settings.max_positions_per_pair,
        max_position_notional_usd=settings.max_position_notional_usd,
        entry_cooldown=timedelta(seconds=seconds),
        mode=settings.mode,
        live_enabled=settings.live_enabled,
    )
