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

    strategies: str = "sma_15m,sma_5m,rsi_15m,bb_15m,rsi_1d,bb_1d"

    sma_fast: int = 20
    sma_slow: int = 50

    bankroll_usd: float = 1000.0
    max_positions_per_pair: int = 5
    max_position_notional_usd: float = 100.0
    # Shared per-leg notional autoscale (all lanes). See snowball.sizing.
    # per_leg = max(BASE, BASE * (1 + (PCT/100) * floor(AV/100))) when enabled.
    per_leg_base_usd: float = 100.0
    per_leg_scale_per_100_usd_pct: float = 1.0
    per_leg_autoscale: bool = True
    daily_loss_kill_usd: float = 25.0
    entry_cooldown_seconds: int = 900
    entry_cooldown_5m_seconds: int = 300
    # Scale-in: second lot only if open lots for that strategy are green by this fraction of entry.
    scale_in_min_profit_pct: float = 0.005
    # Strategy exits (all lanes): never sell red; only take profit at this unrealized fraction.
    # HALT / daily-loss kill still flatten including losers (emergency).
    # Effective strategy/fade floor ≈ MIN_TAKE_PROFIT_PCT + FEE_BUFFER_PCT (0.06+0.01=0.07).
    min_take_profit_pct: float = 0.06
    # SMA strategies (sma_5m / sma_15m / sma_1d): higher floor before strategy/fade exits.
    # Effective ≈ SMA_MIN_TAKE_PROFIT_PCT + FEE_BUFFER_PCT (0.08+0.01=0.09).
    sma_min_take_profit_pct: float = 0.08
    # Round-trip fee estimate (~1%). Exits that would be red after fees are refused.
    fee_buffer_pct: float = 0.01
    # Maker-limit entry/exit settle timeout (seconds). Stale limits are canceled.
    maker_timeout_seconds: float = 90.0
    never_sell_red: bool = True
    never_sell_red_emergency: bool = True
    # Trend filter: require last > SMA slow before any new entry / scale-in.
    trend_filter_enabled: bool = True
    # RSI/BB lean-on filters for entries (block RSI>=70 and close > BB upper).
    indicator_filters_enabled: bool = True
    rsi_period: int = 14
    bb_period: int = 20
    bb_std_mult: float = 2.0
    # Per-product pause after consecutive closed losers.
    pair_pause_enabled: bool = True
    pair_pause_losses: int = 3
    pair_pause_hours: float = 24.0
    # Optional: path to a file listing products (comma or newline) to clear pauses; deleted after apply.
    pair_pause_clear_file: Path | None = None
    slippage_bps: float = 5.0
    taker_fee_bps: float = 0.0
    # Max fraction of total Coinbase account value the crypto lane may deploy (open notional).
    # With CRYPTO_STOCK_SHARED_BUDGET=true (default), crypto+stock share AV*(crypto+stock) (~65%).
    crypto_account_budget_pct: float = 0.40

    sqlite_path: Path = Path("./data/snowball.db")
    heartbeat_path: Path = Path("./data/heartbeat")
    poll_seconds: float = 15.0
    # Coinbase public REST pacing (ms between requests, process-wide via rate_limit).
    ccxt_rate_limit_ms: int = 250
    # Reuse OHLCV within this TTL to cut redundant candle fetches across strategies.
    ohlcv_cache_ttl_sec: float = 45.0
    # Sleep between pair updates in the crypto engine tick (extra spacing).
    pair_fetch_gap_sec: float = 0.15
    public_fetch_retries: int = 4
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
    # Comma-separated @handles; default prioritizes Trading Fraternity, The Stock Market,
    # and Ellio Trades Official.
    youtube_channel_handles: str = (
        "thetradingfraternity,thestockmarket,elliotrades_official"
    )
    # When handles are set, keyword search is off unless this is true (saves quota).
    youtube_allow_keyword_search: bool = False
    # Per priority-channel poll page size (uploads playlistItems, 1 quota unit).
    youtube_priority_max_results: int = 20
    # One-shot Jan→now backfill since (ISO). Empty disables auto backfill.
    # Meta yolo_youtube_backfill_done tracks completed handles per since (JSON);
    # newly added handles backfill without --force / re-walking old channels.
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

    # --- STOCK lane (isolated book; live = CFM US5/TEK index products) ---
    stock_enabled: bool = True
    # Dual gate for live stock: STOCK_MODE=live AND STOCK_LIVE_ENABLED=true
    stock_mode: Literal["paper", "live"] = "paper"
    stock_live_enabled: bool = False
    stock_sqlite_path: Path = Path("./data/snowball_stocks.db")
    stock_bankroll_usd: float = 1000.0
    # Live CFM: prefer 1 lot per index (same spirit as CFM_MAX_CONTRACTS)
    stock_max_positions: int = 1
    # Soft INTX-era cap; CFM live floors sizing with this + 1-contract margin
    stock_max_notional_usd: float = 4000.0
    stock_daily_loss_kill_usd: float = 25.0
    # Fraction of total Coinbase account value stock lane may use (CFM CDE, lev=1).
    # With CRYPTO_STOCK_SHARED_BUDGET=true, stock draws from the shared spot pool with crypto.
    stock_account_budget_pct: float = 0.15
    # When true: crypto+stock share one AV*(crypto_pct+stock_pct) pool (~65%); either lane
    # may use idle capital from the other. When false: independent per-lane budgets.
    crypto_stock_shared_budget: bool = True
    # Live watchlist / mapped products (CFM CDE only — not single-name INTX)
    stock_products: str = "US5-19DEC30-CDE,TEK-19DEC30-CDE"
    stock_strategies: str = "sma_15m,sma_5m,sma_1d,ema_15m,donchian_1d,rsi_15m,bb_15m,rsi_1d,bb_1d"
    stock_poll_seconds: float = 60.0
    # Cap symbols for paper/research universe (live orders only on stock_products)
    stock_max_active: int = 60
    stock_dynamic_max: int = 30
    # CFM-only intraday stall take-profit (additional early bank; swing floors unchanged).
    # When unrealized >= STOCK_CFM_STALL_EXIT_PCT (gross) AND 15m price looks stalled
    # during US cash hours, exit with reason stall_take_profit. Trending → hold for fade.
    stock_cfm_stall_exit_enabled: bool = True
    stock_cfm_stall_exit_pct: float = 0.05
    stock_cfm_stall_lookback_bars: int = 5
    stock_cfm_stall_new_high_tol: float = 0.002
    stock_cfm_stall_range_compress_pct: float = 0.006
    stock_cfm_stall_start_et: str = "09:30"
    stock_cfm_stall_end_et: str = "15:45"
    entry_cooldown_1d_seconds: int = 86400

    # --- Earnings Scout (Nasdaq calendar research sidecar; never trades) ---
    earnings_enabled: bool = True
    earnings_poll_seconds: float = 14400.0
    earnings_sqlite_path: Path = Path("./data/snowball_earnings.db")

    # --- Future Trader (Coinbase CFM CDE index perps; session day-trade; dual-gated live) ---
    futures_enabled: bool = True
    futures_mode: Literal["paper", "live"] = "paper"
    # Dual gate for live futures: FUTURES_MODE=live AND FUTURES_LIVE_ENABLED=true
    futures_live_enabled: bool = False
    futures_sqlite_path: Path = Path("./data/snowball_futures.db")
    futures_bankroll_usd: float = 1000.0
    # Max 1 open lot per index (session engine)
    futures_max_positions: int = 1
    # Soft ceiling per leg (INTX-era); CFM uses integer contracts + margin gates instead
    futures_max_notional_usd: float = 4000.0
    futures_daily_loss_kill_usd: float = 25.0
    # Fraction of total Coinbase account value FT may use (split 50/50 across products)
    futures_account_budget_pct: float = 0.40
    # session_day = overnight-capable ET session; momentum_15m = intraday impulse (shared ~30% budget)
    futures_strategies: str = "session_day,momentum_15m"
    # CFM CDE: US 500 PERP + TECH PERP (not INTX *-PERP-INTX)
    futures_products: str = "US5-19DEC30-CDE,TEK-19DEC30-CDE"
    futures_poll_seconds: float = 60.0
    # America/New_York windows (HH:MM). Entry: preferred 09:25-09:30; late catch-up until exit
    # when momentum is OFF. With momentum_15m enabled, session entry is preferred-window only.
    futures_entry_start_et: str = "09:25"
    futures_entry_end_et: str = "09:30"
    futures_exit_start_et: str = "15:55"
    futures_exit_end_et: str = "16:00"
    # Intraday momentum knobs (used when momentum_15m is in FUTURES_STRATEGIES)
    futures_momentum_timeframe: str = "15m"  # 5m or 15m
    futures_momentum_lookback_bars: int = 8
    futures_momentum_min_pct: float = 0.003  # breakout above prior lookback high
    futures_momentum_take_profit_pct: float = 0.008  # bank green impulse
    futures_momentum_stall_exit_enabled: bool = True
    futures_momentum_stall_lookback_bars: int = 4
    futures_momentum_stall_exit_pct: float = 0.004  # stall bank if >= this green
    # Aspirational FT KPI (log/report only — never overrides risk gates / CFM_MAX_CONTRACTS)
    futures_daily_pnl_target_usd: float = 100.0

    # --- Crash Guard (short hedge on CFM US500/TECH; dual-gated live) ---
    crash_enabled: bool = True
    crash_mode: Literal["paper", "live"] = "paper"
    # Dual gate for live crash shorts: CRASH_MODE=live AND CRASH_LIVE_ENABLED=true
    crash_live_enabled: bool = False
    crash_sqlite_path: Path = Path("./data/snowball_crash.db")
    crash_bankroll_usd: float = 1000.0
    # Max 1 open short lot per index
    crash_max_positions: int = 1
    crash_max_notional_usd: float = 4000.0
    crash_daily_loss_kill_usd: float = 25.0
    # Fraction of total Coinbase account value Crash Guard may use (50/50 across products)
    crash_account_budget_pct: float = 0.10
    crash_products: str = "US5-19DEC30-CDE,TEK-19DEC30-CDE"
    crash_poll_seconds: float = 60.0

    # --- Fed Desk (CME FedWatch research + FOMC skew bets; dual-gated live) ---
    fed_enabled: bool = True
    fed_mode: Literal["paper", "live"] = "paper"
    # Dual gate for live Fed Desk: FED_MODE=live AND FED_LIVE_ENABLED=true
    fed_live_enabled: bool = False
    fed_sqlite_path: Path = Path("./data/snowball_fed.db")
    fed_bankroll_usd: float = 1000.0
    # Max 1 open lot per index
    fed_max_positions: int = 1
    fed_max_notional_usd: float = 4000.0
    fed_daily_loss_kill_usd: float = 25.0
    # Fraction of total Coinbase account value Fed Desk may use (50/50 across products)
    fed_account_budget_pct: float = 0.05
    fed_products: str = "US5-19DEC30-CDE,TEK-19DEC30-CDE"
    fed_poll_seconds: float = 60.0
    # Research poll cadence (FedWatch); trading loop uses fed_poll_seconds
    fed_research_poll_seconds: float = 10800.0

    # --- CFM CDE contract sizing (FT / Crash / Fed / Stock live) ---
    # Max integer contracts per index until Tb raises it. Large notionals (~$3k US500).
    cfm_max_contracts: int = 1
    # API leverage string for Advanced Trade FUTURE orders (keep low; do not force 20x).
    cfm_leverage: float = 1.0
    # Conservative margin estimate vs overnight CFM rates (~7%); used for affordability gates.
    cfm_margin_rate: float = 0.10

    coinbase_api_key: str = ""
    coinbase_api_secret: str = ""
    coinbase_api_passphrase: str = ""

    @field_validator("mode", "stock_mode", "futures_mode", "crash_mode", "fed_mode", mode="before")
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
        fut_ids = parse_strategies(self.futures_strategies)
        if self.futures_enabled and not fut_ids:
            raise ValueError(
                "FUTURES_STRATEGIES must list at least one strategy when futures enabled"
            )
        fut_unknown = [s for s in fut_ids if s not in KNOWN_STRATEGY_IDS]
        if fut_unknown:
            raise ValueError(
                f"Unknown FUTURES_STRATEGIES {fut_unknown}; known: {sorted(KNOWN_STRATEGY_IDS)}"
            )
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
        if strategy_id in ("sma_1d", "donchian_1d", "rsi_1d", "bb_1d"):
            return self.entry_cooldown_1d_seconds
        # sma_15m, ema_15m, rsi_15m, bb_15m share the 15m candle cooldown
        return self.entry_cooldown_seconds

    @property
    def stock_strategy_list(self) -> list[str]:
        return parse_strategies(self.stock_strategies)

    @property
    def stock_product_list(self) -> list[str]:
        """Live CFM index products (normalized). Defaults to US5 + TEK."""
        from snowball.futures.market import normalize_futures_product

        items = [p.strip() for p in self.stock_products.split(",") if p.strip()]
        raw = items or ["US5-19DEC30-CDE", "TEK-19DEC30-CDE"]
        return [normalize_futures_product(p) for p in raw]

    def assert_stock_config(self) -> None:
        """Refuse inconsistent dual-gate; allow paper or fully dual-gated live."""
        if not self.stock_enabled:
            return
        if self.stock_mode == "live" and not self.stock_live_enabled:
            raise LiveTradingRefused(
                "Stock trading refused: STOCK_MODE=live but STOCK_LIVE_ENABLED "
                "is false (both required for live CFM index orders)."
            )
        if self.stock_mode != "live" and self.stock_live_enabled:
            raise LiveTradingRefused(
                "Stock trading refused: STOCK_LIVE_ENABLED=true while "
                "STOCK_MODE is not live (dual gate required)."
            )
        if not (0.0 < float(self.stock_account_budget_pct) <= 1.0):
            raise LiveTradingRefused(
                f"STOCK_ACCOUNT_BUDGET_PCT must be in (0,1]; got {self.stock_account_budget_pct}"
            )

    def assert_stock_paper_only(self) -> None:
        """Back-compat: enforce dual-gate consistency (paper or live both OK)."""
        self.assert_stock_config()

    def stock_live_orders_permitted(self) -> bool:
        """Stock live CFM path requires BOTH stock flags. Default False."""
        return self.stock_mode == "live" and self.stock_live_enabled is True

    def min_take_profit_pct_for(self, strategy_id: str) -> float:
        """Per-strategy min TP: SMA ids use sma_min_take_profit_pct; else global."""
        from snowball.maker import min_take_profit_for_strategy

        return min_take_profit_for_strategy(
            strategy_id,
            min_take_profit_pct=self.min_take_profit_pct,
            sma_min_take_profit_pct=self.sma_min_take_profit_pct,
        )

    def effective_min_take_profit_pct(self) -> float:
        """Global (non-SMA) floor: min_take_profit_pct + fee_buffer_pct."""
        from snowball.allocation import effective_take_profit_floor

        return effective_take_profit_floor(self.min_take_profit_pct, self.fee_buffer_pct)

    def effective_min_take_profit_pct_for(self, strategy_id: str) -> float:
        """Strategy/fade exit floor vs entry including fee buffer."""
        from snowball.allocation import effective_take_profit_floor

        return effective_take_profit_floor(
            self.min_take_profit_pct_for(strategy_id), self.fee_buffer_pct
        )

    def effective_per_leg_notional_usd(self, account_value_usd: float) -> float:
        """Per-leg notional for all lanes from total account value (see sizing.py)."""
        from snowball.sizing import per_leg_notional_usd

        return per_leg_notional_usd(
            account_value_usd,
            base_usd=self.per_leg_base_usd,
            scale_per_100_usd_pct=self.per_leg_scale_per_100_usd_pct,
            autoscale=self.per_leg_autoscale,
        )

    def lane_budget_pcts(self) -> dict[str, float]:
        from snowball.allocation import lane_budget_pcts

        return lane_budget_pcts(
            crypto_pct=self.crypto_account_budget_pct,
            stock_pct=self.stock_account_budget_pct,
            futures_pct=self.futures_account_budget_pct,
            crash_pct=self.crash_account_budget_pct,
            fed_pct=self.fed_account_budget_pct,
            crypto_stock_shared=self.crypto_stock_shared_budget,
        )

    @property
    def futures_strategy_list(self) -> list[str]:
        return parse_strategies(self.futures_strategies)

    @property
    def futures_product_list(self) -> list[str]:
        items = [p.strip().upper() for p in self.futures_products.split(",") if p.strip()]
        return items or ["US5-19DEC30-CDE", "TEK-19DEC30-CDE"]

    def assert_futures_config(self) -> None:
        """Refuse inconsistent dual-gate; allow paper or fully dual-gated live."""
        if not self.futures_enabled:
            return
        if self.futures_mode == "live" and not self.futures_live_enabled:
            raise LiveTradingRefused(
                "Futures trading refused: FUTURES_MODE=live but FUTURES_LIVE_ENABLED "
                "is false (both required for live futures orders)."
            )
        if self.futures_mode != "live" and self.futures_live_enabled:
            raise LiveTradingRefused(
                "Futures trading refused: FUTURES_LIVE_ENABLED=true while "
                "FUTURES_MODE is not live (dual gate required)."
            )
        if not (0.0 < float(self.futures_account_budget_pct) <= 1.0):
            raise LiveTradingRefused(
                f"FUTURES_ACCOUNT_BUDGET_PCT must be in (0,1]; got {self.futures_account_budget_pct}"
            )

    def assert_futures_paper_only(self) -> None:
        """Back-compat alias: enforce dual-gate consistency (paper or live both OK)."""
        self.assert_futures_config()

    def futures_live_orders_permitted(self) -> bool:
        """Future live path requires BOTH futures flags. Default False."""
        return self.futures_mode == "live" and self.futures_live_enabled is True

    def cfm_order_leverage(self) -> float:
        """Conservative CFM leverage for Advanced Trade FUTURE orders (default 1x)."""
        return max(1.0, float(getattr(self, "cfm_leverage", 1.0) or 1.0))

    def cfm_max_contracts_per_index(self) -> int:
        return max(1, int(getattr(self, "cfm_max_contracts", 1) or 1))

    def futures_uses_session_engine(self) -> bool:
        """True when session_day is configured (overnight roll path)."""
        return "session_day" in self.futures_strategy_list

    def futures_uses_momentum(self) -> bool:
        """True when momentum_15m is configured (intraday impulse path)."""
        return "momentum_15m" in self.futures_strategy_list

    def futures_session_entry_preferred_only(self) -> bool:
        """When momentum shares the lane, session enters only in the preferred window."""
        return self.futures_uses_session_engine() and self.futures_uses_momentum()


    @property
    def crash_product_list(self) -> list[str]:
        items = [p.strip().upper() for p in self.crash_products.split(",") if p.strip()]
        return items or ["US5-19DEC30-CDE", "TEK-19DEC30-CDE"]

    def assert_crash_config(self) -> None:
        """Refuse inconsistent dual-gate; allow paper or fully dual-gated live."""
        if not self.crash_enabled:
            return
        if self.crash_mode == "live" and not self.crash_live_enabled:
            raise LiveTradingRefused(
                "Crash Guard refused: CRASH_MODE=live but CRASH_LIVE_ENABLED "
                "is false (both required for live crash shorts)."
            )
        if self.crash_mode != "live" and self.crash_live_enabled:
            raise LiveTradingRefused(
                "Crash Guard refused: CRASH_LIVE_ENABLED=true while "
                "CRASH_MODE is not live (dual gate required)."
            )
        if not (0.0 < float(self.crash_account_budget_pct) <= 1.0):
            raise LiveTradingRefused(
                f"CRASH_ACCOUNT_BUDGET_PCT must be in (0,1]; got {self.crash_account_budget_pct}"
            )

    def crash_live_orders_permitted(self) -> bool:
        """Crash live shorts require BOTH crash flags. Default False."""
        return self.crash_mode == "live" and self.crash_live_enabled is True


    @property
    def fed_product_list(self) -> list[str]:
        items = [p.strip().upper() for p in self.fed_products.split(",") if p.strip()]
        return items or ["US5-19DEC30-CDE", "TEK-19DEC30-CDE"]

    def assert_fed_config(self) -> None:
        """Refuse inconsistent dual-gate; allow paper or fully dual-gated live."""
        if not self.fed_enabled:
            return
        if self.fed_mode == "live" and not self.fed_live_enabled:
            raise LiveTradingRefused(
                "Fed Desk refused: FED_MODE=live but FED_LIVE_ENABLED "
                "is false (both required for live Fed Desk orders)."
            )
        if self.fed_mode != "live" and self.fed_live_enabled:
            raise LiveTradingRefused(
                "Fed Desk refused: FED_LIVE_ENABLED=true while "
                "FED_MODE is not live (dual gate required)."
            )
        if not (0.0 < float(self.fed_account_budget_pct) <= 1.0):
            raise LiveTradingRefused(
                f"FED_ACCOUNT_BUDGET_PCT must be in (0,1]; got {self.fed_account_budget_pct}"
            )

    def fed_live_orders_permitted(self) -> bool:
        """Fed Desk live orders require BOTH fed flags. Default False."""
        return self.fed_mode == "live" and self.fed_live_enabled is True

    def live_orders_permitted(self) -> bool:
        """Live ccxt orders require BOTH flags. Default config returns False."""
        return self.mode == "live" and self.live_enabled is True

    def assert_not_accidentally_live(self) -> None:
        if self.mode == "live" and not self.live_enabled:
            raise LiveTradingRefused(
                "Live trading refused: MODE=live but LIVE_ENABLED is false."
            )
