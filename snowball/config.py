from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from snowball.strategy import KNOWN_STRATEGY_IDS, parse_strategies


DEFAULT_PRODUCTS = ("BTC-USD", "SOL-USD", "ETH-USD", "DOGE-USD")


class LiveTradingRefused(RuntimeError):
    """Raised when code would place a live order without both live gates."""


class Settings(BaseSettings):
    """All runtime knobs. Defaults are paper-only and cannot send live orders."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    mode: Literal["paper", "live"] = "paper"
    live_enabled: bool = False
    trading_enabled: bool = True
    halt_file: Path = Path("./HALT")

    exchange_id: str = "coinbase"
    products: str = "BTC-USD,SOL-USD,ETH-USD,DOGE-USD"
    timeframe: str = "15m"

    strategies: str = "sma_15m,sma_5m"

    sma_fast: int = 20
    sma_slow: int = 50

    bankroll_usd: float = 1000.0
    max_positions_per_pair: int = 5
    max_position_notional_usd: float = 100.0
    daily_loss_kill_usd: float = 25.0
    entry_cooldown_seconds: int = 900
    entry_cooldown_5m_seconds: int = 300
    # Scale-in: second lot only if open lots for that strategy are green by this fraction of entry.
    scale_in_min_profit_pct: float = 0.005
    # Strategy exits (all lanes): never sell red; only take profit at this unrealized fraction.
    # HALT / daily-loss kill still flatten including losers (emergency).
    min_take_profit_pct: float = 0.05
    never_sell_red: bool = True
    never_sell_red_emergency: bool = True
    # Trend filter: require last > SMA slow before any new entry / scale-in.
    trend_filter_enabled: bool = True
    # Per-product pause after consecutive closed losers.
    pair_pause_enabled: bool = True
    pair_pause_losses: int = 3
    pair_pause_hours: float = 24.0
    # Optional: path to a file listing products (comma or newline) to clear pauses; deleted after apply.
    pair_pause_clear_file: Path | None = None
    slippage_bps: float = 5.0
    taker_fee_bps: float = 0.0

    sqlite_path: Path = Path("./data/snowball.db")
    heartbeat_path: Path = Path("./data/heartbeat")
    poll_seconds: float = 15.0
    ohlcv_limit: int = 80
    log_level: str = "INFO"

    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8080
    dashboard_enabled: bool = True

    # --- The Watcher (official macro research sidecar) ---
    watcher_enabled: bool = True
    watcher_poll_seconds: float = 300.0
    tradingeconomics_api_key: str = ""
    fred_api_key: str = ""
    watcher_halt_around_fomc: bool = False
    watcher_halt_minutes_before: int = 30
    watcher_halt_minutes_after: int = 15

    # --- Yolo Demon (YouTube + X research sidecar; never trades) ---
    yolo_demon_enabled: bool = True
    yolo_demon_poll_seconds: float = 120.0
    youtube_api_key: str = ""
    # Comma-separated @handles; default prioritizes Trading Fraternity + The Stock Market.
    youtube_channel_handles: str = "thetradingfraternity,thestockmarket"
    # When handles are set, keyword search is off unless this is true (saves quota).
    youtube_allow_keyword_search: bool = False
    # Per priority-channel poll page size (uploads playlistItems, 1 quota unit).
    youtube_priority_max_results: int = 20
    # One-shot Jan→now backfill since (ISO). Empty disables auto backfill.
    # Meta flag yolo_youtube_backfill_done prevents re-walk every poll.
    yolo_youtube_backfill_since: str = "2026-01-01T00:00:00Z"
    x_bearer_token: str = ""
    # Pay-per-use: even with bearer set, require X_ENABLED=true to call the API.
    x_enabled: bool = False
    x_daily_max_reads: int = 50

    # --- The Clerk (House Clerk PTR research sidecar; never trades) ---
    clerk_enabled: bool = True
    clerk_poll_seconds: float = 21600.0
    clerk_sqlite_path: Path = Path("./data/snowball_clerk.db")
    clerk_pdf_cap: int = 25
    clerk_pdf_delay_seconds: float = 0.75
    # Empty = current calendar year and the prior year (conditional GET, cached).
    clerk_years: str = ""

    # --- STOCK PAPER lane (isolated book; never live equity orders) ---
    stock_enabled: bool = True
    # paper only this week — live stock orders are refused
    stock_mode: Literal["paper", "live"] = "paper"
    stock_sqlite_path: Path = Path("./data/snowball_stocks.db")
    stock_bankroll_usd: float = 1000.0
    stock_max_positions: int = 5
    stock_max_notional_usd: float = 100.0
    stock_daily_loss_kill_usd: float = 25.0
    stock_strategies: str = "sma_15m,sma_5m,sma_1d,ema_15m,donchian_1d"
    stock_poll_seconds: float = 60.0
    # Cap symbols that the paper engine may trade (marks universe may be larger)
    stock_max_active: int = 60
    stock_dynamic_max: int = 30
    entry_cooldown_1d_seconds: int = 86400

    coinbase_api_key: str = ""
    coinbase_api_secret: str = ""
    coinbase_api_passphrase: str = ""

    @field_validator("mode", "stock_mode", mode="before")
    @classmethod
    def _norm_mode(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("pair_pause_clear_file", mode="before")
    @classmethod
    def _empty_path(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return v

    @model_validator(mode="after")
    def _check_sma(self) -> Self:
        if self.sma_fast >= self.sma_slow:
            raise ValueError("SMA_FAST must be < SMA_SLOW")
        if self.sma_fast < 1 or self.sma_slow < 2:
            raise ValueError("SMA periods must be positive")
        ids = parse_strategies(self.strategies)
        if not ids:
            raise ValueError("STRATEGIES must list at least one strategy")
        unknown = [s for s in ids if s not in KNOWN_STRATEGY_IDS]
        if unknown:
            raise ValueError(
                f"Unknown STRATEGIES {unknown}; known: {sorted(KNOWN_STRATEGY_IDS)}"
            )
        stock_ids = parse_strategies(self.stock_strategies)
        if self.stock_enabled and not stock_ids:
            raise ValueError("STOCK_STRATEGIES must list at least one strategy when stock enabled")
        stock_unknown = [s for s in stock_ids if s not in KNOWN_STRATEGY_IDS]
        if stock_unknown:
            raise ValueError(
                f"Unknown STOCK_STRATEGIES {stock_unknown}; known: {sorted(KNOWN_STRATEGY_IDS)}"
            )
        if self.stock_mode != "paper" and self.stock_enabled:
            # Soft: still load but assert_stock_paper_only refuses at runtime.
            pass
        return self

    @property
    def product_list(self) -> list[str]:
        items = [p.strip().upper() for p in self.products.split(",") if p.strip()]
        return items or list(DEFAULT_PRODUCTS)

    @property
    def strategy_list(self) -> list[str]:
        return parse_strategies(self.strategies)

    @property
    def ohlcv_fetch_limit(self) -> int:
        """Enough bars for SMA_SLOW+1 on every fetched timeframe (5m and 15m)."""
        return max(self.ohlcv_limit, self.sma_slow + 1)

    def cooldown_seconds_for(self, strategy_id: str) -> int:
        if strategy_id == "sma_5m":
            return self.entry_cooldown_5m_seconds
        if strategy_id in ("sma_1d", "donchian_1d"):
            return self.entry_cooldown_1d_seconds
        # sma_15m and ema_15m share the 15m candle cooldown
        return self.entry_cooldown_seconds

    @property
    def stock_strategy_list(self) -> list[str]:
        return parse_strategies(self.stock_strategies)

    def assert_stock_paper_only(self) -> None:
        if self.stock_enabled and self.stock_mode != "paper":
            raise LiveTradingRefused(
                f"Stock trading refused: STOCK_MODE={self.stock_mode!r} "
                "(stock lane is paper-only; never places live equity orders)."
            )

    def live_orders_permitted(self) -> bool:
        """Live ccxt orders require BOTH flags. Default config returns False."""
        return self.mode == "live" and self.live_enabled is True

    def assert_not_accidentally_live(self) -> None:
        if self.mode == "live" and not self.live_enabled:
            raise LiveTradingRefused(
                "Live trading refused: MODE=live but LIVE_ENABLED is false."
            )
