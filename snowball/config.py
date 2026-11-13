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
    # Spot dust (Scout idea, Tb 2026-10-01): open lots worth < DUST_USD (mark x qty) do not
    # count toward MAX_POSITIONS_PER_PAIR or the all-lots-green scale-in check, and are
    # never sold. UNTESTED live as of 2026-10-01 (blocked-buy counters in the snapshot).
    dust_usd: float = 5.0
    # Spot USD margin floor (Tb 7:36 AM CT 10/1): block any new spot crypto buy that would take the
    # REAL Coinbase spot USD (exchange free USD, not ledger cash) below this. 0 = off. Sells and the
    # treasury dividend (tools/treasury_dca.py) are unaffected. Lift after the deposit.
    spot_usd_floor: float = 0.0  # Tb dropped the floor 10/2 8:55 AM CT ("forget the cash floor")
    spot_usd_floor_fee_pct: float = 0.006
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
    # Soft-reserve treasury BTC mark out of CRYPTO deployable base; hard-block
    # live BTC sells that would leave wallet below reserved qty. ADD-only
    # contributions (daily profit dividend 3pm CT) unchanged. NEVER_SELL_RED stays on.
    treasury_reserve_enabled: bool = True
    never_sell_treasury: bool = True
    treasury_sqlite_path: Path = Path("./data/snowball_treasury.db")
    # Treasury dip buy (Tb 2026-10-30 4:46 PM CT; live-20261030a): BTC-USD down >= PCT within any
    # rolling WINDOW_MIN -> buy USD of BTC into the treasury (maker-preferred, market ok), once per
    # drop instance. Exempt from SPOT_USD_FLOOR like the dividend; skipped if spot USD < USD.
    treasury_dip_buy_enabled: bool = True
    treasury_dip_buy_pct: float = 0.05
    treasury_dip_buy_window_min: float = 60.0
    treasury_dip_buy_usd: float = 10.0
    # YTD average daily profit (display/goal only, snowball.reports.ytd_profit): the average
    # restarts each year on this America/Chicago month-day (MM-DD); the first counted day is
    # the 3pm->3pm CT window ending on it. 01-01 = Jan 1 (the 24/7 crypto lanes trade on it).
    # An invalid value falls back to 01-01 (reported as year_reset_valid=false), never errors.
    year_reset: str = "01-01"
    # Dividend / daily-profit day cutoff (Tb 9:39 AM CT 10/2): 2 PM -> 3 PM CT, paid at 3 PM CT.
    # Read from the env by snowball.treasury.cutoff (the treasury tool runs without Settings);
    # listed here for docs/validation. Windows ending before the effective CT date keep 2 PM;
    # the effective date's window runs from the previous day's 2 PM (10/2: 25 h); then 24 h.
    dividend_cutoff_hour_ct: int = 15
    dividend_cutoff_effective_ct_date: str = "2026-10-02"
    # Daily net closed profit minimum goal (display/goal only; Tb 2026-10-01: $50 -> $100).
    # Read by snowball.treasury.dca (env DAILY_PROFIT_MIN_USD); never changes trading/sizing.
    daily_profit_min_usd: float = 100.0
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
    # With CRYPTO_STOCK_SHARED_BUDGET=true, crypto+stock share AV*(crypto+stock).
    # Default false: independent per-lane budgets (crypto 40% / stock 20%).
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

    # --- Research proposals (Task 16): research -> proposals -> (confirmed) trades ---
    # Off unless RESEARCH_PROPOSALS_ENABLED=true. Proposals never size orders.
    research_proposals_enabled: bool = False
    # research_idea strategy (autonomous entries on high-confidence proposals);
    # only active when research_proposals_enabled is also true.
    research_idea_enabled: bool = True
    research_min_confidence: float = 0.70
    research_proposal_ttl_hours: float = 24.0
    research_poll_seconds: float = 600.0
    research_proposals_sqlite_path: Path = Path("./data/snowball_proposals.db")
    # Probability of success + auto-execution (Tb 3:45 AM CT 10/3: "execute these plays with a
    # 70% probability of success or higher"). When RESEARCH_PROB_ENABLED, a proposal is executed by
    # research_idea only if probability >= RESEARCH_EXEC_MIN_PROB (and, unless disabled, its
    # expected value after fees is > 0 for the lane's real target/stop); RESEARCH_MIN_CONFIDENCE
    # is then display-only. Below threshold: logged only. Every lane guardrail still applies.
    research_prob_enabled: bool = True
    research_exec_min_prob: float = 0.70
    research_exec_require_positive_ev: bool = True
    research_exec_dedupe_hours: float = 72.0
    research_prob_horizon_hours: float = 72.0
    research_prob_ref_target_pct: float = 0.03
    research_prob_ref_stop_pct: float = 0.03
    research_prob_history_range: str = "180d"
    research_prob_fetch_budget: int = 12
    # --- Forward Guidance (Tb 8:58 AM CT 10/3): regime + continuation probabilities per market ---
    # Daily forecast (multi-year daily history, walk-forward calibrated, persisted + auto-graded in
    # snowball_forward.db), PDF/snapshot "Forward Guidance" section. Runs in its own thread at/after
    # FORWARD_RUN_AFTER_CT (before the 8am PDF) and once at startup when today's run is missing.
    forward_enabled: bool = True
    forward_sqlite_path: Path | None = None  # default: snowball_forward.db next to SQLITE_PATH
    forward_run_after_ct: str = "07:20"
    forward_check_seconds: float = 600.0
    # Spot bear gate ("in a bear regime spot buys should pause or shrink"; spot stays never-sell-red):
    # new spot buys on a product whose 1w AND 1m regimes are bear are paused; bear on one of them ->
    # notional x FORWARD_SPOT_BEAR_SHRINK. Products with too little history use BTC's regime.
    # Fail-open: no forecast in the last FORWARD_MAX_AGE_HOURS -> no gate. Sells/treasury unaffected.
    forward_spot_gate_enabled: bool = True
    forward_spot_bear_shrink: float = 0.5
    forward_max_age_hours: float = 36.0
    # Regime input to research P(success): logit tilt from the 1w regime's directional base rate,
    # applied ONLY for markets whose out-of-sample 1w P(up) skill vs the base rate is >=
    # FORWARD_RESEARCH_MIN_SKILL (so it is off wherever the regime has shown no edge). Capped.
    forward_research_input_enabled: bool = True
    forward_research_min_skill: float = 0.005
    forward_research_max_logit: float = 0.25

    # --- Yolo Demon (YouTube + X research sidecar; never trades) ---
    yolo_demon_enabled: bool = True
    yolo_demon_poll_seconds: float = 120.0
    youtube_api_key: str = ""
    # Comma-separated @handles; default prioritizes Trading Fraternity, The Stock Market,
    # Ellio Trades Official, ZipTrader (@ZipTrader = UC0BGhWsIbV7Dm-lsvhdlMbA) and
    # Clear Value Tax (@clearvaluetax9382 = UCigUBIf-zt_DA6xyOQtq2WA, Tb 2:05 AM CT 10/3).
    youtube_channel_handles: str = (
        "thetradingfraternity,thestockmarket,elliotrades_official,ziptrader,clearvaluetax9382"
    )
    # When handles are set, keyword search is off unless this is true (saves quota).
    youtube_allow_keyword_search: bool = False
    # Per priority-channel poll page size (uploads playlistItems, 1 quota unit).
    youtube_priority_max_results: int = 20
    # One-shot Jan→now backfill since (ISO). Empty disables auto backfill.
    # Meta yolo_youtube_backfill_done tracks completed handles per since (JSON);
    # newly added handles backfill without --force / re-walking old channels.
    yolo_youtube_backfill_since: str = "2026-01-01T00:00:00Z"
    # 2026-09-30: read video transcripts (youtube-transcript-api) in a background
    # thread; polite delay + hourly cap; circuit breaker on 429/IP block.
    yolo_transcripts_enabled: bool = True
    yolo_transcript_delay_seconds: float = 6.0
    yolo_transcript_max_per_hour: int = 300
    # 2026-10-03 (Tb 2:06 AM CT): transcript fallback chain. api = youtube-transcript-api;
    # ytdlp = captions via yt-dlp (manual English, then YouTube auto-captions) when the API is
    # IP-blocked or finds nothing; whisper = no captions anywhere -> audio via yt-dlp + local
    # faster-whisper (nice 19 subprocess, own thread). Each method has its own breaker.
    yolo_transcript_methods: str = "api,ytdlp,whisper"
    yolo_ytdlp_timeout_seconds: float = 180.0
    # Full transcript stored in yolo_transcripts.text (was trimmed to 40k chars). 0 = no cap.
    yolo_transcript_max_stored_chars: int = 2_000_000
    yolo_whisper_enabled: bool = True
    yolo_whisper_model: str = "base.en"
    yolo_whisper_device: str = "cpu"
    yolo_whisper_threads: int = 2
    # Whisper queue: newest first in tiers: published in the last FRESH_HOURS (any length), then
    # normal videos, then livestreams longer than LONG_MINUTES; > MAX_MINUTES never transcribed.
    yolo_whisper_max_minutes: float = 720.0
    yolo_whisper_long_minutes: float = 120.0
    yolo_whisper_fresh_hours: float = 48.0
    yolo_whisper_model_dir: Path = Path("./data/whisper-models")
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
    # Kept small so CFM TEK notional cannot starve the crypto lane when shared is off.
    stock_account_budget_pct: float = 0.20
    # When true: crypto+stock share one AV*(crypto_pct+stock_pct) pool; either lane
    # may use idle capital from the other. Default false: independent per-lane budgets.
    crypto_stock_shared_budget: bool = False
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
    # Net-green close (Scout idea, Tb 2026-10-01 6:25 AM CT): EVERY FT close path (session,
    # momentum TP/stall/EOD, stock stall, research, legacy, fade, HALT and daily-loss flatten)
    # closes only when net P&L after ROUND-TRIP fees > this buffer; otherwise hold. Contract
    # rolls (reason contract_roll, futures/roll.py) keep their own exception, unchanged.
    futures_net_green_buffer_usd: float = 0.50
    # REAL Coinbase CFM fees, derived from our 25 recorded live fills (9/11-9/29): every fill
    # (1 contract, US5 ~$3,080-3,117 and TEK ~$3,902-4,106 notional, maker and market) was
    # charged exactly $0.12, independent of notional -> flat $0.12/contract/side, 0% of notional.
    futures_fee_pct_per_side: float = 0.0
    futures_fee_per_contract: float = 0.12
    # Futures stop-losses (Tb 2026-10-01 6:27 AM CT: "Put stop losses on all futures positions,
    # longs and any shorts") — FUTURES-ONLY exception to never-sell-red/never-cover-red, all lanes
    # (FT, Crash Guard, Fed Desk). Stop at the CLOSER of FUTURES_STOP_PCT x roll-adjusted basis
    # (FUTURES_STOP_PCT_FAMILIES only) or a FUTURES_STOP_USD_PER_CONTRACT loss net of real fees.
    # Bot-side, market close, one order per lot (snowball_stops.db). One-flag rollback:
    # FUTURES_STOPS_ENABLED=false (levels still shown, no stop orders).
    futures_stops_enabled: bool = True
    futures_stop_pct: float = 0.08
    futures_stop_pct_families: str = "US5,TEK"
    futures_stop_usd_per_contract: float = 250.0
    # Liquidation guard: stop a lot when real CFM liquidation headroom per contract <= this.
    futures_liq_guard_usd: float = 50.0
    # 2026-10-30 (Tb "Yes record it and update the code", 5:34 PM CT; image live-20261030a):
    # TEK #21 stop kept reusing a 10/25 6:51 PM CT failed order record (status error, order_id
    # null, PREVIEW_FUTURES_AFTER_HOUR_INVALID_ORDER_TYPE) and "gave up after 3 failed attempts"
    # for the whole 10/30 trigger. A failed/never-acknowledged closing-order record older than
    # FUTURES_STOP_FAILED_TTL_SEC now expires and the next trigger builds a fresh order.
    futures_stop_failed_ttl_sec: float = 900.0
    # Closing-order type: "auto" = market IOC first; when Coinbase rejects market orders
    # (after-hours / non-core session) send a non-post-only GTC limit at the worst touch +/-
    # FUTURES_STOP_LIMIT_COLLAR_PCT (Coinbase accepts/queues limit orders after hours) and keep
    # monitoring it. "limit" = always that limit. "market" = legacy market-only.
    futures_stop_order_type: str = "auto"
    futures_stop_limit_collar_pct: float = 0.005
    # Aged-lot exit (Tb 9:38 AM CT 10/2), all futures lanes (FT, Crash, Fed; long and short):
    # a NON-netted lot older than FUTURES_AGED_LOT_DAYS from its original entry (through contract
    # rolls) closes as soon as its net P&L vs the roll-adjusted basis after real round-trip fees is
    # >= FUTURES_AGED_LOT_MIN_NET_USD. Never closes red because of age (stops stay the only red
    # exits). Bot-side in futures/stops.py: reconcile + exchange-position check, one closing order
    # per lot. One-flag rollback: FUTURES_AGED_EXIT_ENABLED=false.
    futures_aged_exit_enabled: bool = True
    futures_aged_lot_days: float = 3.0
    futures_aged_lot_min_net_usd: float = 0.50
    # Default None = snowball_stops.db next to FUTURES_SQLITE_PATH (./data in the container).
    futures_stops_sqlite_path: Path | None = None
    # Netted pairs (bookkeeping only; Tb confirmed 10/1): Coinbase nets FT TEK #13 long vs Fed TEK
    # #2 short to 0. Both legs excluded from stops, exits, margin math and the one-index gate;
    # no realized P/L booked, no rows deleted, no orders. "lane:id=lane:id[,...]"; "" = none.
    futures_netted_pairs: str = "futures:13=fed:2"
    # Auto-net any remaining same-product long/short overlap across lanes (2026-10-02, e.g. FT US5
    # long vs a Crash/Fed US5 short): Coinbase nets per product, so the pair is bookkeeping-only.
    # 2026-10-10 (Tb "Do all a b and c"): OFF. Auto-netting hid opposite-side lots across lanes
    # (Coinbase nets per product -> hidden realized losses -$164.30, TEK 9/29 + US5 10/1). New
    # opposite-side entries are now blocked instead (OPPOSITE_SIDE_BLOCK). Persisted/explicit
    # netted pairs are still honoured.
    futures_auto_net_overlaps: bool = False
    # One index contract (US5/TEK) at a time across FT/Crash/Fed until ~$1,000 more is deposited
    # (Tb 7:36 AM CT 10/1). Entry gate only; never force-closes; rolls are not entries.
    futures_one_index_contract: bool = True
    futures_index_families: str = "US5,TEK"
    futures_max_index_contracts: int = 1  # FUTURES_MAX_INDEX_CONTRACTS: total open index contracts allowed (2026-10-12)
    # One-index slot reservation (Tb 2026-10-10): "lane:families" ("*" = all index families, i.e.
    # FUTURES_INDEX_FAMILIES). e.g. "futures:*" = only the FT lane may open US5/TEK; Crash/Fed/
    # Stocks entries on those families are blocked (index_slot_reserved:futures). "" = off.
    # The open-lot / exchange-net one-index checks still apply to EVERY lane (incl. FT).
    index_slot_reserved_for: str = ""
    # (A) 2026-10-10: no lane may open the OPPOSITE side of a CFM product that any lane
    # (FT/Crash/Fed/Stocks; spot excluded) already holds open. New entries only.
    opposite_side_block: bool = True
    # (C) 2026-10-10: margin gate. No new futures entry (any lane, live) unless, AFTER the entry,
    # real CFM liquidation-buffer headroom per contract >= the new lot's stop distance
    # (+ FUTURES_LIQ_GUARD_USD when FUTURES_MARGIN_GATE_INCLUDE_LIQ_GUARD) so the stop, not the
    # liquidation guard, limits the loss. Buffer after = buffer - est. margin of the new contract.
    # Fails closed in live when exchange data is unavailable.
    futures_margin_gate: bool = True
    futures_margin_gate_include_liq_guard: bool = True
    # Fraction of total Coinbase account value FT may use (split 50/50 across products)
    futures_account_budget_pct: float = 0.40
    # session_day = overnight-capable ET session; momentum_15m = intraday impulse (shared ~30% budget)
    futures_strategies: str = "session_day,momentum_15m"
    # CFM CDE: US 500 PERP + TECH PERP (not INTX *-PERP-INTX)
    futures_products: str = "US5-19DEC30-CDE,TEK-19DEC30-CDE"
    # session_day (09:25 ET long / overnight roll) only runs on these index products.
    # Added products (BTC, crude) trade momentum_15m only. Default = US5/TEK (unchanged).
    futures_session_products: str = "US5-19DEC30-CDE,TEK-19DEC30-CDE"
    # session_day product PRIORITY (Tb 7:46 AM CT 10/2): when the one-index gate allows only one
    # contract, session_day tries TEK before US5. Comma list of families; unknown ones ignored;
    # order only (gates/stops/net-green/sizes unchanged). Env SESSION_PRODUCT_PRIORITY.
    session_product_priority: str = "TEK,US5"
    # Products the FT momentum lane may SHORT on a downside breakdown (never-cover-red
    # applies). Default empty = long-only (unchanged). e.g. "NOL-19OCT26-CDE".
    futures_short_products: str = ""
    # Block NEW FT entries on dated contracts within this many days of expiry.
    futures_expiry_entry_block_days: float = 3.0
    # 2026-09-30 (Tb): roll EVERY dated CFM contract on every lane near expiry
    # (window = FUTURES_EXPIRY_ENTRY_BLOCK_DAYS). The roll is the only exit path
    # allowed to close red (reason contract_roll); basis is carried forward.
    # Replaces the oil-only FUTURES_OIL_AUTO_ROLL idea. Spot BTC treasury unaffected.
    futures_auto_roll: bool = True
    # 2026-09-30 (Tb, Task 10): the STOCK lane is merged INTO the Future Trader lane.
    # The former stock entry strategies (STOCK_STRATEGIES list unless overridden
    # here) run inside the FT lane on the US5/TEK families, sharing the FT budget
    # (FUTURES_ACCOUNT_BUDGET_PCT), CFM_MAX_CONTRACTS, leverage, contract roll,
    # never-sell-red. ONE NET POSITION PER PRODUCT FAMILY for the whole FT lane.
    futures_stock_merge_enabled: bool = True
    futures_stock_strategies: str = ""  # empty = inherit STOCK_STRATEGIES
    futures_stock_products: str = ""  # empty = inherit STOCK_PRODUCTS (US5,TEK)
    # 2026-10-16 (Tb 6:42 PM CT): merged stock strategies listed here stay in the FT lane and keep
    # managing their OWN open lots (signal exit, fade, stall bank; stops/aged exit are lane-wide),
    # but may NOT open NEW lots. Comma list; empty = none blocked (re-enable = empty).
    # Env FUTURES_STOCK_ENTRY_DISABLED, e.g. "bb_15m". Spot crypto STRATEGIES are unaffected.
    futures_stock_entry_disabled: str = ""
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
    # (B) 2026-10-10: NEW crash shorts need >= CRASH_MIN_TRIGGERS of the 3 triggers AND (when
    # CRASH_REQUIRE_BELOW_SMA) the last price below the CRASH_TREND_SMA_DAYS daily average.
    crash_min_triggers: int = 2
    crash_require_below_sma: bool = True
    crash_trend_sma_days: int = 20
    # (B) Crash cover target: short P&L >= this NET of real round-trip CFM fees (all crash lots,
    # incl. already-open ones; still never covers red). 0 = legacy MIN_TAKE_PROFIT_PCT+FEE_BUFFER_PCT.
    crash_cover_pct: float = 0.025

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
    # (B) 2026-10-10: NEW Fed bets need a real FedWatch shift: the dominant outcome's probability
    # rose by >= FED_MIN_ODDS_SHIFT (0.10 = 10 pts) vs the FedWatch history lookback, AND (when
    # FED_REQUIRE_TREND) trend confirmation on daily closes vs FED_TREND_SMA_DAYS average (short:
    # last < SMA = downtrend; long: last > SMA). A high level alone no longer enters.
    fed_min_odds_shift: float = 0.10
    fed_require_trend: bool = True
    fed_trend_sma_days: int = 20
    # (B) Fed take-profit: P&L >= this NET of real round-trip CFM fees (never red). 0 = legacy.
    fed_cover_pct: float = 0.025
    # 2026-10-16: optional note shown on the dashboard when a lane is switched off with
    # FED_ENABLED=false / CRASH_ENABLED=false (e.g. "pending re-evaluation"). Display only.
    fed_disabled_note: str = ""
    # 2026-10-16 6:55 PM CT (Tb: "we still need to be getting the fedwatch data"): the FedWatch
    # research poll (FED_RESEARCH_POLL_SECONDS, writes snowball_fed.db research_snapshots + meta)
    # keeps running when FED_ENABLED=false. Research only: no market client, no ledger, no orders.
    # Env FED_RESEARCH_ENABLED (default true). Ignored when FED_ENABLED=true (full lane polls).
    fed_research_enabled: bool = True
    crash_disabled_note: str = ""

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

    @property
    def futures_stock_strategy_list(self) -> list[str]:
        """Former STOCK-lane strategies now run inside the FT lane (Task 10)."""
        raw = (self.futures_stock_strategies or "").strip()
        ids = parse_strategies(raw) if raw else list(self.stock_strategy_list)
        primary = {"session_day", "momentum_15m"}
        return [s for s in ids if s not in primary]

    @property
    def futures_stock_entry_disabled_list(self) -> list[str]:
        """Merged stock strategies blocked from NEW FT entries (exits still managed)."""
        raw = self.futures_stock_entry_disabled or ""
        return [s.strip().lower() for s in raw.split(",") if s.strip()]

    @property
    def futures_stock_product_list(self) -> list[str]:
        from snowball.futures.market import normalize_futures_product

        raw = [p.strip() for p in (self.futures_stock_products or "").split(",") if p.strip()]
        return [normalize_futures_product(p) for p in raw] if raw else list(self.stock_product_list)

    @property
    def session_product_priority_list(self) -> list[str]:
        out: list[str] = []
        for p in str(self.session_product_priority or "").split(","):
            fam = p.strip().upper().split("-", 1)[0]
            if fam and fam not in out:
                out.append(fam)
        return out

    @property
    def futures_session_product_list(self) -> list[str]:
        return [p.strip().upper() for p in self.futures_session_products.split(",") if p.strip()]

    @property
    def futures_short_product_list(self) -> list[str]:
        return [p.strip().upper() for p in self.futures_short_products.split(",") if p.strip()]

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
