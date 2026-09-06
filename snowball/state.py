from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from snowball.config import Settings
from snowball.models import PairSnapshot, utcnow
from snowball.paper import PaperLedger
from snowball.live import LiveBroker
from snowball.watcher.store import WatcherStore
from snowball.yolo_demon.store import YoloStore


@dataclass
class AppState:
    settings: Settings
    ledger: PaperLedger
    broker: LiveBroker | None = None
    pairs: dict[str, PairSnapshot] = field(default_factory=dict)
    last_tick_at: datetime | None = None
    last_error: str | None = None
    started_at: datetime = field(default_factory=utcnow)
    running: bool = True
    lock: threading.RLock = field(default_factory=threading.RLock)
    watcher: WatcherStore | None = None
    yolo: YoloStore | None = None
    watcher_sidecar: object | None = None
    yolo_sidecar: object | None = None
    # --- STOCK PAPER (isolated; never mixes with crypto ledger) ---
    stock_ledger: PaperLedger | None = None
    stock_pairs: dict[str, PairSnapshot] = field(default_factory=dict)
    stock_last_tick_at: datetime | None = None
    stock_mark_source: str = "yahoo_paper"
    stock_universe_all: list[str] = field(default_factory=list)
    stock_universe_active: list[str] = field(default_factory=list)
    stock_universe_dynamic: list[str] = field(default_factory=list)
    stock_universe_sources: dict = field(default_factory=dict)
    stock_coinbase_ids: dict[str, str] = field(default_factory=dict)
    stock_engine: object | None = None

    def marks(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for product, snap in self.pairs.items():
            if snap.last is not None:
                out[product] = snap.last
        return out
