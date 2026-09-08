# Snowball

24/7 Coinbase trading bot (default **paper**). Long-only 20/50 SMA crossover on **15-minute and 5-minute** candles for **BTC-USD, SOL-USD, ETH-USD, DOGE-USD**. Paper fills are simulated locally from live public prices. Dual-gated live (`MODE=live` **and** `LIVE_ENABLED=true`) places Coinbase Advanced Trade market orders via ccxt; default config **cannot place live orders**.

Target host: Ubuntu 24.04 LTS, CPU + network only (no CUDA).

## What it does

- Pulls public Coinbase prices via ccxt (`exchange id: coinbase`).
- Paper broker: marketable buy/sell at last (else mid) plus configurable slippage, persisted in sqlite under `data/` so restarts keep cash, lots, and fills.
- Strategies (comma list `STRATEGIES`, default `sma_15m,sma_5m`): enter when SMA 20 crosses above SMA 50. Both lanes **never sell red** on strategy logic; `MIN_TAKE_PROFIT_PCT` (default 5%) is a floor only — full death-cross (`EXIT`) closes all eligible lots, and a momentum fade (`last < SMA_fast` while still `> SMA_slow`) scales out **one** best-green lot (`{strategy}:fade`). No auto-sell at 5% without fade or exit. Multi-day holds and scale-in toward max lots are allowed. Optional scale-in while still in an uptrend after that strategy's cooldown **and** open lots are green by `SCALE_IN_MIN_PROFIT_PCT` (default 0.5%). A strategy only exits lots it opened.
- New entries / scale-ins also require `last > SMA_SLOW` when `TREND_FILTER_ENABLED=true` (default).
- Per-product auto-pause after `PAIR_PAUSE_LOSSES` consecutive closed losers (default 3) for `PAIR_PAUSE_HOURS` (default 24). Exits still allowed. Clear via `PAIR_PAUSE_CLEAR_FILE` or wait it out. Dashboard shows paused pairs.
- Scorecard in `/api/snapshot` and the dashboard: closed trades / win rate / realized PnL / open count per strategy and per product (measurement only).
- Risk (enforced in code):
  - Virtual bankroll $1000 USD
  - Max **5 open positions per pair across all strategies** (book max 20)
  - Per-position notional cap **$100**
  - Daily realized + unrealized loss kill **$25** (flatten if allowed, then block new entries for the rest of the UTC day)
  - Cooldown is per (pair, strategy): ~15 minutes for `sma_15m`, ~5 minutes for `sma_5m`
- Kill switch: if `HALT` exists, the engine **emergency-flattens** open lots (may sell red; logged as `emergency flatten sells red`) then blocks new entries while keeping market logs. `TRADING_ENABLED=false` places **zero orders** (no flatten). Delete `HALT` to resume.
- Live path is dual-gated: `MODE=paper` and `LIVE_ENABLED=false` by default. Live market orders require **both** `MODE=live` and `LIVE_ENABLED=true` plus Coinbase key/secret. `MODE=live` without `LIVE_ENABLED` still refuses at startup. Exchange-confirmed fills are recorded into the same sqlite ledger so risk gates and the dashboard keep working.
- LAN dashboard on port 8080 (no auth). Read-only except Halt/Resume, which only writes/deletes the HALT file in paper mode. **No control that enables live trading.**

This is not Coinbase’s static sandbox. Paper is a local fill simulator on live public market data.

**Coinbase cash note:** free cash for `*-USD` pairs must be **USD**. A USDC balance will not fund USD-quoted pairs until converted to USD.


## Architecture (short)

```
ccxt public tickers/OHLCV ──► engine tick (15s)
                                │
                                ├─ SMA 20/50 on 15m and 5m closes
                                ├─ risk gates (halt / daily kill / caps / per-strategy cooldown)
                                ├─ paper ledger (sqlite fills + cash + lots, tagged by strategy)
                                └─ in-memory snapshot ──► FastAPI :8080 (SSE)
```

One process: engine thread + dashboard + **The Watcher** (official-macro research) + **Yolo Demon** (YouTube / X research) + **The Clerk** (House Clerk PTR research). systemd or Docker runs that process. `systemctl stop snowball` is the LAN off switch. Research sidecars never place orders.

```
official RSS/APIs ──► The Watcher thread (300s) ──► sqlite research_events
YouTube/X        ──► Yolo Demon thread (120s) ──► sqlite yolo_ideas
House Clerk PTRs ──► The Clerk thread (>=6h) ──► sqlite data/snowball_clerk.db
                      (same process; Clerk uses its own db; dashboard read-only panels)
```


## Strategies

`STRATEGIES` is a comma list (default `sma_15m,sma_5m`). Unknown names abort at startup.

| Id | Timeframe | Cooldown env | Default | Exit policy |
| --- | --- | --- | --- | --- |
| `sma_15m` | 15-minute candles | `ENTRY_COOLDOWN_SECONDS` | 900s | Never sell red; exit/fade ≥5% |
| `sma_5m` | 5-minute candles | `ENTRY_COOLDOWN_5M_SECONDS` | 300s | Never sell red; exit/fade ≥5% |
| `ema_15m` | 15-minute candles | `ENTRY_COOLDOWN_SECONDS` | 900s | Stock paper only. EMA 12/26 cross. Never sell red; exit/fade ≥5% |
| `donchian_1d` | daily candles | `ENTRY_COOLDOWN_1D_SECONDS` | 86400s | Stock paper only. 20-day high break / 10-day low exit. Never sell red; exit/fade ≥5% |

Same 20/50 periods (`SMA_FAST` / `SMA_SLOW`). Max 5 open lots **per pair across all strategies**. Strategy exits require `MIN_TAKE_PROFIT_PCT` (default 0.05) **plus** a death cross (all eligible) or momentum fade (one lot); HALT / daily-loss kill still flatten including losers. Enable both (already default):

```bash
# both (default)
STRATEGIES=sma_15m,sma_5m
# 15m only
STRATEGIES=sma_15m
# 5m only
STRATEGIES=sma_5m
```

Leave `MODE=paper` and `LIVE_ENABLED=false`.

## Paper run (venv)

```bash
sudo timedatectl set-ntp true   # or chrony; see clock note below
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # leave MODE=paper LIVE_ENABLED=false
python -m snowball
```

Dashboard: from this machine open http://127.0.0.1:8080  
From another machine on the LAN: `http://<host-ip>:8080` (bind is `0.0.0.0` by default).

**Do not expose port 8080 to the internet.** There is no authentication.

## Halt / resume / off

| Action | How |
| --- | --- |
| Emergency flatten + block entries | `touch HALT` (or dashboard **Halt**; may sell red) |
| Resume orders | `rm HALT` (or dashboard **Resume**) |
| Disable orders via env | `TRADING_ENABLED=false` (restart if set only in `.env`) |
| Stop the process | `sudo systemctl stop snowball` or `docker compose down` |

Clearing `HALT` resumes. A daily-loss kill stays in effect until the next UTC day.

## Docker

```bash
cp .env.example .env
docker compose build
docker compose up -d
```

Healthcheck hits `http://127.0.0.1:8080/health` inside the container. State lives in the `snowball-data` volume (`HALT` path is `/app/data/HALT`).

## systemd (Ubuntu 24.04)

```bash
sudo useradd --system --home /opt/snowball --shell /usr/sbin/nologin snowball
sudo mkdir -p /opt/snowball
sudo rsync -a --exclude .venv ./ /opt/snowball/
sudo chown -R snowball:snowball /opt/snowball
sudo -u snowball python3.12 -m venv /opt/snowball/.venv
sudo -u snowball /opt/snowball/.venv/bin/pip install -e /opt/snowball
sudo cp /opt/snowball/systemd/snowball.service /etc/systemd/system/snowball.service
sudo systemctl daemon-reload
sudo systemctl enable --now snowball
journalctl -u snowball -f
```

Stop (LAN off switch): `sudo systemctl stop snowball`.

## Clock sync

Candle timestamps and any future live API signatures assume a sane clock. On Ubuntu 24.04 enable NTP (systemd-timesyncd or Chrony):

```bash
sudo timedatectl set-ntp true
# or: sudo apt install chrony && sudo systemctl enable --now chrony
timedatectl status
```

## Coinbase Advanced API key (live only)

Only if you intentionally leave paper mode — not required to run this bot.

1. Coinbase Advanced Trade API (CDP / Advanced Trade), **not** the abandoned static sandbox.
2. Permissions: **view + trade**. **No withdraw / transfer**.
3. Restrict the key to your host’s public IP.
4. Put the key in `.env` (`COINBASE_API_KEY`, `COINBASE_API_SECRET`). CDP `apiKey` looks like `organizations/.../apiKeys/...`; secret is an EC PEM (literal `\n` escapes in `.env` are expanded before ccxt). Never commit `.env`.
5. Live orders require **both** `MODE=live` and `LIVE_ENABLED=true` (and keys). `.env.example` keeps paper defaults.

### Live checklist (short)

1. Fund the Coinbase account used by the API key (USD).
2. Confirm paper has been healthy (HALT / daily kill / dashboard).
3. In `.env` set keys, then `MODE=live` and `LIVE_ENABLED=true` (both required).
4. Restart the bot process (`systemctl restart snowball` or equivalent).
5. Watch logs for `LIVE broker constructed` and `live buy` / `live sell`; dashboard still reads the sqlite ledger.
6. Soft stop: `touch HALT` or `TRADING_ENABLED=false`. Hard stop: `systemctl stop snowball`.
7. To return to paper: set `MODE=paper`, `LIVE_ENABLED=false`, restart.

Watcher + Yolo Demon stay research-only and never place orders.

## The Watcher (official-macro research)

Same process as the paper bot. Ingests **official RSS only** (no HTML scrape) from the Fed, ECB, Bank of England, Bank of Japan, and TreasuryDirect, plus optional Trading Economics calendar and FRED rate snapshots. Writes `research_events` in `data/snowball.db`. This table is the shared bus a future stock bot can read. **The Watcher never places orders.**

Default `WATCHER_ENABLED=true`. Poll every `WATCHER_POLL_SECONDS` (300). User-Agent `SnowballWatcher/1.0`.

Optional APIs (skip cleanly if unset — do not invent keys):

1. **Trading Economics calendar** — create a key at [developer.tradingeconomics.com](https://developer.tradingeconomics.com) and set `TRADINGECONOMICS_API_KEY` in `.env`. Docs: [calendar snapshot](https://docs.tradingeconomics.com/economic_calendar/snapshot/). The Watcher does not scrape tradingeconomics.com HTML and does not use a guest key.
2. **FRED** — request a key at [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html) (`FRED_API_KEY`). Series: `FEDFUNDS`, `DGS2`, `DGS10`, `DFF`.

World Bank news RSS was dead (404) at build time and is skipped; HTML news pages are not scraped.

Optional auto-HALT around high-importance FOMC / rate-decision calendar windows is **off** by default (`WATCHER_HALT_AROUND_FOMC=false`). Dashboard Halt is enough for v1. If you enable it, The Watcher writes the same `HALT` file and a sibling `WATCHER_HALT` flag so it will not delete a halt you created by hand.

Dashboard panel **The Watcher**: upcoming calendar (next 48h) + latest official press. Links out. No buy/sell controls.

## The Clerk (House congressional disclosure research)

Same process as the trading bot. Polls the official House Clerk yearly index
`https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip`
at most once per 6 hours (default `CLERK_POLL_SECONDS=21600`) and logs
`FilingType=P` Periodic Transaction Reports. Electronic PTR PDFs are parsed
with `pdftotext`. Scanned paper (DocIDs starting 8 or 9, or empty text) is
logged with `parse_status=scanned_skip` and not OCR'd.

Own database: `data/snowball_clerk.db`. Does not write the crypto ledger, the
stock paper ledger, or `HALT`. **The Clerk never places orders.**

Dashboard panel **The Clerk** plus `/api/clerk` snapshot key. Watchlist names
are surfaced first; other House P filings are indexed, with new PDF downloads
capped (`CLERK_PDF_CAP`, default 25).

## Yolo Demon (multi-source research)

Separate sidecar (`yolo_demon`). Pulls **official APIs only** — YouTube Data API v3 and X (Twitter) API v2. No HTML scrape, no posting, never places orders.

**StockTwits dropped:** StockTwits no longer issues API keys, so it is not an active source.

**HIGH RISK / RESEARCH ONLY / DOES NOT TRADE.** Yolo Demon must not place orders, write `HALT`, or change SMA/risk.

### How to get API access

| Source | Env | How |
| --- | --- | --- |
| **YouTube** | `YOUTUBE_API_KEY` | Google Cloud → enable **YouTube Data API v3** → create an API key. |
| **X / Twitter** | `X_BEARER_TOKEN` + `X_ENABLED=true` | X developer console, pay-per-use credits, bearer token. **Cost ~$0.005/post read.** |

**YouTube priority channels (every video):** default `YOUTUBE_CHANNEL_HANDLES=thetradingfraternity,thestockmarket` ([@thetradingfraternity](https://youtube.com/@thetradingfraternity), [@thestockmarket](https://youtube.com/@thestockmarket)).

- Resolves handle → `channelId` + **uploads playlist** (`channels.list` `contentDetails`, ~1 quota unit), then `playlistItems.list` (~1 unit/page, 50 videos) — much cheaper than `search.list` (100 units/call).
- **Every-video rule (priority handles only):** each upload is stored in sqlite `yolo_videos` even with zero `$TICKER` cashtags. Mentions use extracted tickers, or synthetic `WATCH` when none. Keyword search (if enabled) still requires tickers and does **not** write `yolo_videos`.
- **Backfill:** on first poll (or `python -m snowball.yolo_demon.backfill`), walks uploads from `YOLO_YOUTUBE_BACKFILL_SINCE` (default `2026-01-01T00:00:00Z`) through now. Meta `yolo_youtube_backfill_done` prevents re-walking every poll; afterward only recent pages are fetched (`YOUTUBE_PRIORITY_MAX_RESULTS`, default 20).
- Free daily quota is ~10k units; prefer playlist walks. When handles are set, **keyword search is skipped** unless `YOUTUBE_ALLOW_KEYWORD_SEARCH=true`.

Dashboard **Priority channel videos** lists title, channel, published time, link, and any tickers found.

**X is OFF by default:** even with `X_BEARER_TOKEN` set, require `X_ENABLED=true`. Hard daily cap `X_DAILY_MAX_READS=50` (posts returned, tracked per UTC day in sqlite). Never commit secrets.

Stores mention heat in sqlite `yolo_ideas` (`source`, ticker, score, window, sample_title, url, fetched_at). Deduped by `(source, ticker, window)`. Crypto tickers (`BTC`, `ETH`, `DOGE`, `SOL`) are tagged for display only. Priority watch rows live in `yolo_videos` (keyed by `video_id`).

Default `YOLO_DEMON_ENABLED=true` (no-ops when all source keys are missing). Poll ~120s.

This sidecar is part of the shared research bus for a future stock bot. Paper still cannot live-trade.

## Offline backtest (no paper ledger writes)

Simulates current SMA enter/exit **with** the new gates (trend filter + scale-in green), and prints a separate ATR-trail candidate line for comparison. Does not place paper orders.

```bash
# fixture (used by tests)
python -m snowball.backtest --csv tests/fixtures/ohlcv_sample.csv

# optional live OHLCV fetch for manual exploration only
python -m snowball.backtest --live-fetch --product BTC-USD --timeframe 15m --limit 200
```



## Stock paper trial (HOT watchlist)

Isolated **STOCK PAPER** lane (`STOCK_MODE=paper` only). Uses a second sqlite book (`data/snowball_stocks.db`) with its own bankroll / cash / PnL / daily-loss kill — **never mixed** with the crypto live ledger.

### Marks / product ids

Coinbase Advanced Trade under the current CDP key exposes equity **perps** (`*-PERP-INTX`), not spot US equities. The stock lane therefore marks from **Yahoo Finance public data** (`mark_source=yahoo_paper`) and **never places live stock orders**.

### Universe

1. **Top 25 SPY holdings by weight** — [stockanalysis.com/etf/spy/holdings](https://stockanalysis.com/etf/spy/holdings/) as of 2026-08-19  
2. **Top 25 QQQ holdings by weight** (Nasdaq-100 proxy) — [stockanalysis.com/etf/qqq/holdings](https://stockanalysis.com/etf/qqq/holdings/) as of 2026-08-27  
3. Deduplicate overlap (keep first occurrence)  
4. Overlay prior **core** `SPY,QQQ,IWM,AAPL,MSFT,NVDA,TSLA,SPCX` and **chip/AI** `AMD,AVGO,SMCI,TSM,ARM,PLTR,SOUN,AI`  
5. Overlay **Yolo Demon** dynamic tickers from priority YouTube videos / ideas (skip crypto + junk; cap `STOCK_DYNAMIC_MAX`, default 30)  
6. Cap actively traded symbols at `STOCK_MAX_ACTIVE` (default 60); remaining symbols stay in the mark universe metadata

### Risk (stock book only)

Same discipline as crypto paper: never sell red on strategy logic, `MIN_TAKE_PROFIT_PCT=0.05`, momentum-fade scale-out, max 5 lots/symbol (`STOCK_MAX_POSITIONS`), $100/leg (`STOCK_MAX_NOTIONAL_USD`), daily loss kill $25 (`STOCK_DAILY_LOSS_KILL_USD`). Strategies: `sma_15m` / `sma_5m` when intraday bars exist; `sma_1d` (SMA 20/50 on daily); plus stock-paper `ema_15m` (EMA 12/26 on 15m) and `donchian_1d` (20-day Donchian breakout / 10-day low exit). Crypto live stays `sma_5m`/`sma_15m` only.

### Dashboard

Section **Stock Paper** plus `GET /api/stocks`. Crypto `MODE` / `LIVE_ENABLED` are unchanged.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Tests mock market data; they do not need the network. Backtest tests use CSV fixtures only.

## Defaults that block live orders

- `MODE=paper`
- `LIVE_ENABLED=false`
- Engine never calls `create_order` on ccxt in paper mode
- `LiveBroker` / `make_broker` raise `LiveTradingRefused` unless both live flags are set (and keys for construction)
- `MODE=live` with `LIVE_ENABLED=false` still refuses at startup
